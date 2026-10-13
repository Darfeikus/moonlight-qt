#include "usbipdevices.h"

#include "settings/streamingpreferences.h"

#ifdef Q_OS_LINUX
#include "streaming/usbip/usbipclient.h"
#endif

#include <QMap>
#include <algorithm>

UsbipDevices::UsbipDevices(StreamingPreferences* preferences, QObject* parent)
    : QObject(parent),
      m_Preferences(preferences)
{
}

bool UsbipDevices::isSupported()
{
#ifdef Q_OS_LINUX
    return true;
#else
    return false;
#endif
}

void UsbipDevices::refresh()
{
#ifdef Q_OS_LINUX
    UsbipClient* client = UsbipClient::get();
    if (!m_Connected) {
        connect(client, &UsbipClient::listFinished,
                this, &UsbipDevices::handleListFinished, Qt::QueuedConnection);
        m_Connected = true;
    }

    m_Pending++;
    emit stateChanged();
    client->requestDevices();
#endif
}

void UsbipDevices::setSelected(const QString& id, bool selected)
{
    QStringList current = m_Preferences->usbipDevices;
    if (selected) {
        current.append(id);
    }
    else {
        current.removeAll(id);
    }

    QStringList normalized = StreamingPreferences::normalizeUsbipDevices(current);
    if (normalized != m_Preferences->usbipDevices) {
        m_Preferences->usbipDevices = normalized;
        emit m_Preferences->usbipDevicesChanged();
    }
}

void UsbipDevices::handleListFinished(bool ok, const QVariantList& devices, const QString& error)
{
    if (m_Pending > 0) {
        m_Pending--;
    }

    // One row per vendor:product; a repeated ID can't be told apart by the
    // service, so it is flagged and not offered for selection.
    QMap<QString, QVariantMap> byId;
    for (const QVariant& entry : ok ? devices : QVariantList()) {
        QVariantMap device = entry.toMap();
        QString id = device.value("vendor").toString() + ":" + device.value("product").toString();
        if (byId.contains(id)) {
            continue;
        }
        byId.insert(id, {
            {"id", id},
            {"name", device.value("name").toString()},
            {"busid", device.value("busid").toString()},
            {"connected", true},
            {"duplicate", device.value("count").toInt() > 1},
        });
    }

    // Keep saved devices visible when they are unplugged or the service is down
    for (const QString& id : std::as_const(m_Preferences->usbipDevices)) {
        if (!byId.contains(id)) {
            byId.insert(id, {
                {"id", id},
                {"name", QString()},
                {"busid", QString()},
                {"connected", false},
                {"duplicate", false},
            });
        }
    }

    QList<QVariantMap> sorted = byId.values();
    std::sort(sorted.begin(), sorted.end(), [](const QVariantMap& a, const QVariantMap& b) {
        bool aConnected = a.value("connected").toBool();
        if (aConnected != b.value("connected").toBool()) {
            return aConnected;
        }
        return QString::localeAwareCompare(a.value("name").toString(), b.value("name").toString()) < 0;
    });

    QVariantList rows;
    for (const QVariantMap& row : std::as_const(sorted)) {
        rows.append(row);
    }
    m_Devices = rows;
    emit devicesChanged();

    m_Error = ok ? QString() : error;
    emit stateChanged();
}
