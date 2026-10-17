#pragma once

#include <QObject>
#include <QString>
#include <QVariantList>

class StreamingPreferences;

// QML-facing list of USB devices that the USB/IP service can hand to the
// streaming PC. Only functional on Linux; elsewhere `supported` is false.
class UsbipDevices : public QObject
{
    Q_OBJECT

    Q_PROPERTY(bool supported READ isSupported CONSTANT)
    Q_PROPERTY(bool loading READ isLoading NOTIFY stateChanged)
    Q_PROPERTY(QString error READ error NOTIFY stateChanged)
    // Entries: id ("vendor:product"), name, busid, connected, duplicate
    Q_PROPERTY(QVariantList devices READ devices NOTIFY devicesChanged)

public:
    explicit UsbipDevices(StreamingPreferences* preferences, QObject* parent = nullptr);

    static bool isSupported();

    bool isLoading() const { return m_Pending > 0; }
    QString error() const { return m_Error; }
    QVariantList devices() const { return m_Devices; }

    Q_INVOKABLE void refresh();
    Q_INVOKABLE void setSelected(const QString& id, bool selected);

signals:
    void stateChanged();
    void devicesChanged();

private slots:
    void handleListFinished(bool ok, const QVariantList& devices, const QString& error);

private:
    StreamingPreferences* m_Preferences;
    QVariantList m_Devices;
    QString m_Error;
    int m_Pending = 0;
    bool m_Connected = false;
};
