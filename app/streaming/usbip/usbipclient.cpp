#include "usbipclient.h"

#include "SDL_compat.h"

#include <QDeadlineTimer>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLocalSocket>
#include <QMutexLocker>

namespace {

constexpr int kConnectTimeoutMs = 2000;
// A capture runs several SSH round trips inside the service
constexpr int kReplyTimeoutMs = 90000;
constexpr qsizetype kMaxReplyBytes = 1 << 20;

UsbipClient* s_Instance = nullptr;
QMutex s_InstanceLock;

}

UsbipClient* UsbipClient::get()
{
    QMutexLocker locker(&s_InstanceLock);
    if (s_Instance == nullptr) {
        s_Instance = new UsbipClient();
        s_Instance->start();
    }
    return s_Instance;
}

void UsbipClient::shutdown(int timeoutMs)
{
    UsbipClient* instance;
    {
        QMutexLocker locker(&s_InstanceLock);
        instance = s_Instance;
        s_Instance = nullptr;
    }
    if (instance == nullptr) {
        return;
    }

    // Let the final release go out before the process exits
    instance->waitForIdle(timeoutMs);
    {
        QMutexLocker locker(&instance->m_Lock);
        instance->m_Stopping = true;
        instance->m_WorkAvailable.wakeAll();
    }
    if (instance->wait(2000)) {
        delete instance;
    }
}

QString UsbipClient::socketPath()
{
    QString override = qEnvironmentVariable("MOONLIGHT_USBIP_SOCKET");
    return override.isEmpty() ? QStringLiteral("/run/usbip-bind/control.sock") : override;
}

void UsbipClient::submit(const QString& action, const QString& host, const QString& user,
                         const QString& vendor, const QString& product)
{
    SDL_LogInfo(SDL_LOG_CATEGORY_APPLICATION,
                "USB/IP %s %s:%s for host %s (ssh user: %s)",
                action.toUtf8().constData(),
                vendor.toUtf8().constData(),
                product.toUtf8().constData(),
                host.toUtf8().constData(),
                user.isEmpty() ? "default" : user.toUtf8().constData());

    enqueue({QJsonObject{
        {"action", action},
        {"host", host},
        {"user", user},
        {"vendor", vendor},
        {"product", product},
    }, false});
}

void UsbipClient::requestDevices()
{
    enqueue({QJsonObject{{"action", "list"}}, true});
}

void UsbipClient::enqueue(const Job& job)
{
    QMutexLocker locker(&m_Lock);
    m_Queue.enqueue(job);
    m_Pending++;
    m_WorkAvailable.wakeOne();
}

bool UsbipClient::waitForIdle(int timeoutMs)
{
    QMutexLocker locker(&m_Lock);
    QDeadlineTimer deadline(timeoutMs);
    while (m_Pending > 0) {
        if (!m_Idle.wait(&m_Lock, deadline)) {
            return false;
        }
    }
    return true;
}

void UsbipClient::run()
{
    for (;;) {
        Job job;
        {
            QMutexLocker locker(&m_Lock);
            while (m_Queue.isEmpty() && !m_Stopping) {
                m_WorkAvailable.wait(&m_Lock);
            }
            if (m_Queue.isEmpty()) {
                return;
            }
            job = m_Queue.dequeue();
        }

        QJsonObject result;
        QString error;
        bool ok = transact(job.request, result, error);
        QByteArray action = job.request.value("action").toString().toUtf8();

        if (job.isList) {
            QVariantList devices;
            if (ok) {
                devices = result.value("devices").toArray().toVariantList();
            }
            emit listFinished(ok, devices, error);
        }
        else if (ok) {
            SDL_LogInfo(SDL_LOG_CATEGORY_APPLICATION,
                        "USB/IP %s succeeded", action.constData());
        }
        else {
            SDL_LogError(SDL_LOG_CATEGORY_APPLICATION,
                         "USB/IP %s failed: %s",
                         action.constData(), error.toUtf8().constData());
        }

        QMutexLocker locker(&m_Lock);
        m_Pending--;
        if (m_Pending == 0) {
            m_Idle.wakeAll();
        }
    }
}

bool UsbipClient::transact(const QJsonObject& request, QJsonObject& result, QString& error)
{
    QLocalSocket socket;
    socket.connectToServer(socketPath(), QIODevice::ReadWrite);
    if (!socket.waitForConnected(kConnectTimeoutMs)) {
        error = QStringLiteral("USB/IP service is not reachable at %1: %2")
                    .arg(socketPath(), socket.errorString());
        return false;
    }

    socket.write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
    if (!socket.waitForBytesWritten(kConnectTimeoutMs)) {
        error = QStringLiteral("Failed to send the USB/IP request: %1").arg(socket.errorString());
        return false;
    }

    QByteArray reply;
    QDeadlineTimer deadline(kReplyTimeoutMs);
    for (;;) {
        // The service may reply and close before we get to wait
        reply += socket.readAll();
        if (reply.contains('\n')) {
            break;
        }
        if (reply.size() > kMaxReplyBytes) {
            error = QStringLiteral("USB/IP service reply is too large");
            return false;
        }
        if (!socket.waitForReadyRead(int(qMax<qint64>(deadline.remainingTime(), 0)))) {
            error = socket.state() == QLocalSocket::UnconnectedState
                        ? QStringLiteral("USB/IP service closed the connection")
                        : QStringLiteral("Timed out waiting for the USB/IP service");
            return false;
        }
    }

    QJsonParseError parseError;
    QJsonDocument document = QJsonDocument::fromJson(reply.left(reply.indexOf('\n')), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        error = QStringLiteral("Invalid reply from the USB/IP service");
        return false;
    }

    QJsonObject response = document.object();
    if (!response.value("ok").toBool()) {
        error = response.value("error").toString(QStringLiteral("Unknown USB/IP service error"));
        return false;
    }

    result = response.value("result").toObject();
    return true;
}
