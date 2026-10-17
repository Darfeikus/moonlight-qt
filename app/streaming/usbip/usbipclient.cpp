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

QString deviceOf(const QJsonObject& request)
{
    return request.value("vendor").toString() + ':' + request.value("product").toString();
}

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

    {
        QMutexLocker locker(&instance->m_Lock);
        instance->m_Queue.clear();
        instance->m_Stopping = true;
        instance->m_WorkAvailable.wakeAll();
    }

    // If a request is still running, exiting closes the connection anyway and
    // the service releases what it captured once it finishes.
    if (instance->wait(timeoutMs)) {
        delete instance;
    }
}

QString UsbipClient::socketPath()
{
    QString override = qEnvironmentVariable("MOONLIGHT_USBIP_SOCKET");
    return override.isEmpty() ? QStringLiteral("/run/usbip-bind/control.sock") : override;
}

void UsbipClient::submit(const QString& action, const QString& host, const QString& user,
                         const QString& platform, const QString& computerUuid,
                         const QString& vendor, const QString& product)
{
    SDL_LogInfo(SDL_LOG_CATEGORY_APPLICATION,
                "USB/IP %s %s:%s for host %s (ssh user: %s)",
                action.toUtf8().constData(),
                vendor.toUtf8().constData(),
                product.toUtf8().constData(),
                host.toUtf8().constData(),
                user.isEmpty() ? "default" : user.toUtf8().constData());

    QJsonObject request{
        {"action", action},
        {"host", host},
        {"user", user},
        {"vendor", vendor},
        {"product", product},
    };
    if (action == "capture") {
        request.insert("platform", platform);
    }
    enqueue({request, computerUuid, false});
}

void UsbipClient::requestDevices()
{
    enqueue({QJsonObject{{"action", "list"}}, QString(), true});
}

void UsbipClient::enqueue(const Job& job)
{
    QMutexLocker locker(&m_Lock);

    if (!job.isList) {
        // A capture and a release of the same device that have not started yet
        // cancel out, so quick toggles never hand the device over for nothing.
        QString device = deviceOf(job.request);
        for (qsizetype i = m_Queue.size() - 1; i >= 0; i--) {
            const Job& queued = m_Queue.at(i);
            if (queued.isList || deviceOf(queued.request) != device) {
                continue;
            }
            if (queued.request.value("action") != job.request.value("action")) {
                m_Queue.removeAt(i);
                return;
            }
            break;
        }
    }

    m_Queue.enqueue(job);
    m_WorkAvailable.wakeOne();
}

void UsbipClient::run()
{
    // Owned by this thread; the service ties our captures to this connection
    QLocalSocket* socket = nullptr;

    for (;;) {
        Job job;
        {
            QMutexLocker locker(&m_Lock);
            while (m_Queue.isEmpty() && !m_Stopping) {
                m_WorkAvailable.wait(&m_Lock);
            }
            if (m_Stopping) {
                break;
            }
            job = m_Queue.dequeue();

            QString known = job.computerUuid.isEmpty() ? QString() : m_Platforms.value(job.computerUuid);
            if (!known.isEmpty() && job.request.contains("platform")) {
                job.request.insert("platform", known);
            }
        }

        QJsonObject result;
        QString error;
        bool ok = transact(socket, job.request, result, error);
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

            QString platform = result.value("platform").toString();
            if (!platform.isEmpty() && !job.computerUuid.isEmpty() &&
                    platform != job.request.value("platform").toString()) {
                {
                    QMutexLocker locker(&m_Lock);
                    m_Platforms.insert(job.computerUuid, platform);
                }
                emit platformDetected(job.computerUuid, platform);
            }
        }
        else {
            SDL_LogError(SDL_LOG_CATEGORY_APPLICATION,
                         "USB/IP %s failed: %s",
                         action.constData(), error.toUtf8().constData());
        }
    }

    delete socket;
}

bool UsbipClient::transact(QLocalSocket*& socket, const QJsonObject& request,
                           QJsonObject& result, QString& error)
{
    QJsonObject response;

    // The service may have restarted since the last request, closing our idle
    // connection. Requests are idempotent, so retry once on a fresh connection.
    for (int attempt = 0;; attempt++) {
        bool reused = socket != nullptr;
        if (socket == nullptr) {
            socket = new QLocalSocket();
            socket->connectToServer(socketPath(), QIODevice::ReadWrite);
            if (!socket->waitForConnected(kConnectTimeoutMs)) {
                error = QStringLiteral("USB/IP service is not reachable at %1: %2")
                            .arg(socketPath(), socket->errorString());
                delete socket;
                socket = nullptr;
                return false;
            }
        }

        if (exchange(socket, request, response, error)) {
            break;
        }

        bool dropped = socket->state() == QLocalSocket::UnconnectedState;

        // Never reuse a connection after a failed exchange: a late reply would
        // be read as the answer to the next request. Closing it also makes the
        // service release anything this connection captured.
        socket->abort();
        delete socket;
        socket = nullptr;
        if (!reused || !dropped || attempt > 0) {
            return false;
        }
    }

    if (!response.value("ok").toBool()) {
        error = response.value("error").toString(QStringLiteral("Unknown USB/IP service error"));
        return false;
    }

    result = response.value("result").toObject();
    return true;
}

bool UsbipClient::exchange(QLocalSocket* socket, const QJsonObject& request,
                           QJsonObject& response, QString& error)
{
    socket->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
    if (!socket->waitForBytesWritten(kConnectTimeoutMs)) {
        error = QStringLiteral("Failed to send the USB/IP request: %1").arg(socket->errorString());
        return false;
    }

    QByteArray reply;
    QDeadlineTimer deadline(kReplyTimeoutMs);
    for (;;) {
        // The reply may already be buffered before we get to wait
        reply += socket->readAll();
        if (reply.contains('\n')) {
            break;
        }
        if (reply.size() > kMaxReplyBytes) {
            error = QStringLiteral("USB/IP service reply is too large");
            return false;
        }
        if (!socket->waitForReadyRead(int(qMax<qint64>(deadline.remainingTime(), 0)))) {
            error = socket->state() == QLocalSocket::UnconnectedState
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

    response = document.object();
    return true;
}
