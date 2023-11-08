# AWS Distribution for OpenTelemetry Collector (as configured by Dwolla)

[![](https://images.microbadger.com/badges/image/dwolla/otel-collector.svg)](https://microbadger.com/images/dwolla/otel-collector)
[![license](https://img.shields.io/github/license/dwolla/dwolla-adot-collector.svg?style=flat-square)](https://github.com/Dwolla/dwolla-adot-collector/blob/master/LICENSE)

Docker image that adds Dwolla's configuration to the AWS distribution of OpenTelemetry collector.

## Local Development

With [yq](https://kislyuk.github.io/yq/) installed, to build this image locally run the following command:

```bash
make \
    TAG=$(curl --silent https://raw.githubusercontent.com/Dwolla/jenkins-agents-workflow/main/.github/workflows/build-docker-image.yml | \
        yq .on.workflow_call.inputs.NVM_TAG.default) \
    all
```

Alternatively, without [yq](https://kislyuk.github.io/yq/) installed, refer to the NVM_TAG default values defined in [jenkins-agents-workflow](https://github.com/Dwolla/jenkins-agents-workflow/blob/main/.github/workflows/build-docker-image.yml) and run the following command:

`make NVM_TAG=<default-nvm-tag-from-jenkins-agents-workflow> all`
