import QtQuick 2.9
import QtQuick.Controls 2.2

import StreamingPreferences 1.0
import SdlGamepadKeyNavigation 1.0
import SystemProperties 1.0
import UsbipDevices 1.0

// Multiselect of the USB devices that are handed to the streaming PC (through
// the USB/IP service) while the mouse is captured.
Row {
    id: devicePicker
    spacing: 8
    visible: UsbipDevices.supported

    readonly property string summaryText: {
        var ids = StreamingPreferences.usbipDevices
        if (ids.length === 0) {
            return qsTr("None")
        }
        if (ids.length === 1) {
            var devices = UsbipDevices.devices
            for (var i = 0; i < devices.length; i++) {
                if (devices[i].id === ids[0] && devices[i].name !== "") {
                    return devices[i].name
                }
            }
            return ids[0]
        }
        return qsTr("%1 devices").arg(ids.length)
    }

    Component.onCompleted: {
        if (UsbipDevices.supported) {
            UsbipDevices.refresh()
        }
    }

    Label {
        id: pickerLabel
        anchors.verticalCenter: pickerButton.verticalCenter
        text: qsTr("USB devices to capture:")
        font.pointSize: 12
    }

    Button {
        id: pickerButton
        hoverEnabled: true
        text: devicePicker.summaryText
        font.pointSize: 12

        onClicked: {
            UsbipDevices.refresh()
            devicesPopup.open()
        }

        ToolTip.delay: 1000
        ToolTip.timeout: 10000
        ToolTip.visible: hovered
        ToolTip.text: qsTr("USB devices (for example a wireless mouse receiver) that are passed to the streaming PC while your mouse is captured, and returned when it is released.") + "\n\n" +
                      qsTr("Requires the USB/IP service from this repository's README.")

        Popup {
            id: devicesPopup
            y: pickerButton.height
            width: Math.max(pickerButton.width, 420)
            padding: 8
            focus: true
            closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

            onAboutToShow: {
                // Switch to normal navigation inside the popup, like combo boxes
                SdlGamepadKeyNavigation.setUiNavMode(false)
                if (SystemProperties.usesMaterial3Theme) {
                    background.color = "#424242"
                }
            }

            onOpened: {
                deviceList.currentIndex = 0
                deviceList.forceActiveFocus()
            }

            onAboutToHide: {
                SdlGamepadKeyNavigation.setUiNavMode(true)
            }

            contentItem: Column {
                spacing: 6

                Label {
                    width: parent.width
                    visible: UsbipDevices.error !== ""
                    wrapMode: Text.Wrap
                    color: "#ff8a80"
                    text: UsbipDevices.error
                }

                Label {
                    width: parent.width
                    visible: UsbipDevices.error === "" && deviceList.count === 0
                    text: UsbipDevices.loading ? qsTr("Looking for USB devices...") : qsTr("No USB devices found")
                }

                ListView {
                    id: deviceList
                    width: parent.width
                    height: Math.min(contentHeight, 320)
                    visible: count > 0
                    clip: true
                    focus: true
                    keyNavigationEnabled: true
                    model: UsbipDevices.devices

                    function toggleCurrent() {
                        if (currentItem && currentItem.enabled) {
                            UsbipDevices.setSelected(currentItem.device.id, !currentItem.selected)
                        }
                    }

                    Keys.onSpacePressed: toggleCurrent()
                    Keys.onReturnPressed: toggleCurrent()
                    Keys.onEnterPressed: toggleCurrent()

                    ScrollBar.vertical: ScrollBar {}

                    delegate: CheckDelegate {
                        property var device: modelData
                        readonly property bool selected: StreamingPreferences.usbipDevices.indexOf(device.id) >= 0

                        width: deviceList.width
                        font.pointSize: 12
                        hoverEnabled: true
                        highlighted: ListView.isCurrentItem
                        checked: selected
                        // A repeated vendor:product can't be told apart, so it can only be unselected
                        enabled: !device.duplicate || selected
                        text: (device.name !== "" ? device.name : qsTr("Unknown device")) +
                              "  [" + device.id + "]" +
                              (device.connected ? "" : "  - " + qsTr("not connected"))

                        onToggled: UsbipDevices.setSelected(device.id, checked)

                        ToolTip.delay: 500
                        ToolTip.timeout: 8000
                        ToolTip.visible: hovered && device.duplicate
                        ToolTip.text: qsTr("Several connected devices share this ID, so it can't be selected safely.")
                    }
                }
            }
        }
    }
}
