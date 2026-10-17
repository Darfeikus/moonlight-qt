#pragma once

#include <QJsonObject>
#include <QMutex>
#include <QQueue>
#include <QString>
#include <QThread>
#include <QVariantList>
#include <QWaitCondition>

// Talks to the root usbip-bind.service over its local socket. Requests run one
// at a time on a worker thread so captures and releases stay ordered and never
// block the input, render or UI threads.
class UsbipClient : public QThread
{
    Q_OBJECT

public:
    static UsbipClient* get();

    // Drains queued requests (bounded by timeoutMs) and stops the worker
    static void shutdown(int timeoutMs);

    static QString socketPath();

    // action is "capture" or "release"; host is the address Moonlight uses
    void submit(const QString& action, const QString& host, const QString& user,
                const QString& vendor, const QString& product);

    // Result arrives through listFinished()
    void requestDevices();

    bool waitForIdle(int timeoutMs);

signals:
    void listFinished(bool ok, const QVariantList& devices, const QString& error);

protected:
    void run() override;

private:
    struct Job
    {
        QJsonObject request;
        bool isList;
    };

    UsbipClient() = default;

    void enqueue(const Job& job);
    bool transact(const QJsonObject& request, QJsonObject& result, QString& error);

    QMutex m_Lock;
    QWaitCondition m_WorkAvailable;
    QWaitCondition m_Idle;
    QQueue<Job> m_Queue;
    int m_Pending = 0;
    bool m_Stopping = false;
};
