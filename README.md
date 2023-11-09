# AWS Distribution for OpenTelemetry Collector (as configured by Dwolla)

[![](https://images.microbadger.com/badges/image/dwolla/otel-collector.svg)](https://microbadger.com/images/dwolla/otel-collector)
[![license](https://img.shields.io/github/license/dwolla/dwolla-adot-collector.svg?style=flat-square)](https://github.com/Dwolla/dwolla-adot-collector/blob/master/LICENSE)

Docker image that adds Dwolla's configuration to the AWS distribution of OpenTelemetry collector.

## Local Development

With [jq](https://jqlang.github.io/jq/manual/) installed, to build this image locally run the following command:

```bash
make \
    OTEL_TAG=$(curl --silent https://api.github.com/repos/aws-observability/aws-otel-collector/releases/latest | jq -r .name) \
    all
```
