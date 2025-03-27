# ESP32 Sensor Monitoring - Device Status Dashboard

This dashboard provides a visual overview of all ESP32 sensor devices in the 3A console monitoring system. It helps you track the deployment status, template versions, and update requirements for your IoT sensor network.
[Device status website](https://clausen-engineering.github.io/3a-console-sensor-device-status/)

## Features

- **Device Overview**: See all devices at a glance with their current status
- **Version Tracking**: Monitor which template version each device is using
- **Deployment Status**: Track when devices were last deployed
- **Update Notifications**: Easily identify which devices need updates
- **Filtering**: Filter devices by status (Up to date, Needs update, Unknown)
- **Search**: Find specific devices by name or characteristics

## Getting Started

### Prerequisites

- Web server to host the dashboard (or view locally)
- Up-to-date `device-status.json` data file

### Setup

1. Clone this repository
2. Ensure the `data/device-status.json` file is populated with current device data
3. Open `index.html` in a web browser or deploy to your web server

### Updating Device Data

The dashboard reads device data from `data/device-status.json`. This file should be updated whenever:

- New devices are added to the system
- Devices are deployed with new firmware
- Template versions are updated
- Device configurations change

You can update this file manually or set up automated generation using the `scripts/compare-versions.sh` script from the ESP32 template repository.

## Understanding Status Indicators

- **Up to date**: Device is running the latest template version
- **Needs update**: Device template version is behind the latest available version
- **Unknown**: Version information is missing or cannot be determined

## Deployment Workflow Integration

This dashboard works best as part of your overall device management workflow:

1. Use `scripts/compare-versions.sh` to check which devices need updates
2. Update device configurations as needed
3. Use `scripts/update-device-version.sh` to update version tracking information
4. Deploy updated configurations with `scripts/deploy-device.sh`
5. Refresh the status dashboard to confirm successful deployments

## Custom Development

### Modifying the Dashboard

The dashboard is built with:
- HTML5
- Bootstrap 5.2.3 for styling
- Chart.js for visualizations
- Vanilla JavaScript

To customize:
1. Edit `index.html` for layout and structure changes
2. Modify the embedded CSS for styling adjustments
3. Update the JavaScript for behavioral changes

### Data Structure

The `device-status.json` file uses the following structure:

```json
{
  "latest_version": "v1.1.3",
  "repo_name": "3a-console-esp32-template-sm",
  "last_commit_date": "2025-03-15 20:47:36",
  "last_updated": "2025-03-15 19:47:53",
  "devices": [
    {
      "name": "device-name",
      "version": "v1.1.3",
      "status": "Needs update",
      "location": "Location description",
      "last_updated": "2025-03-15",
      "last_deployed": "2025-03-15",
      "notes": "Notes about this device"
    },
    // Additional devices...
  ]
}
```

## Troubleshooting

### Dashboard Not Loading

- Check that `device-status.json` exists in the `data` directory
- Verify the JSON structure is valid
- Check browser console for JavaScript errors

### Incorrect Status Information

- Ensure `device-status.json` is up to date
- Run `scripts/compare-versions.sh` to generate current status information
- Verify that version.json files exist for all devices

## Contributing

Contributions to improve the dashboard are welcome. Please follow these steps:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.
