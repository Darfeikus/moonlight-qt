#pragma once

#include <QHash>
#include <QJsonObject>
#include <QMutex>
#include <QQueue>
#include <QString>
#include <QThread>
#include <QVariantList>
#include <QWaitCondition>

class QLocalSocket;

// Talks to the root usbip-bind.service over one long-lived local socket. The
// service releases every device this connection captured as soon as it closes,
// so quitting, crashing or being killed all give the devices back. Requests run
// one at a time on a worker thread so captures and releases stay ordered and
// never block the input, render or UI threads.
class UsbipClient : public QThread
{
    Q_OBJECT

public:
    static UsbipClient* get();

    // Drops queued requests and stops the worker, waiting at most timeoutMs for
    // a request already in progress. Closing the connection releases whatever
    // is still captured, so nothing queued needs to be sent.
    static void shutdown(int timeoutMs);

    static QString socketPath();

    // action is "capture" or "release"; host is the address Moonlight uses.
    // platform is the OS saved for that PC ("windows", "linux", or empty to have
    // the service detect it), and computerUuid identifies the PC for
    // platformDetected().
    void submit(const QString& action, const QString& host, const QString& user,
                const QString& platform, const QString& computerUuid,
                const QString& vendor, const QString& product);

    // Result arrives through listFinished()
    void requestDevices();

signals:
    void listFinished(bool ok, const QVariantList& devices, const QString& error);

    // The service found a PC's OS to differ from the one Moonlight sent
    void platformDetected(const QString& computerUuid, const QString& platform);

protected:
    void run() override;

private:
    struct Job
    {
        QJsonObject request;
        QString computerUuid;
        bool isList;
    };

    UsbipClient() = default;

    void enqueue(const Job& job);
    bool transact(QLocalSocket*& socket, const QJsonObject& request,
                  QJsonObject& result, QString& error);
    bool exchange(QLocalSocket* socket, const QJsonObject& request,
                  QJsonObject& response, QString& error);

    QMutex m_Lock;
    QWaitCondition m_WorkAvailable;
    QQueue<Job> m_Queue;
    // Latest OS the service reported per PC, so later captures skip detection
    QHash<QString, QString> m_Platforms;
    bool m_Stopping = false;
};
