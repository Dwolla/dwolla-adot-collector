OTEL_TAG := $(OTEL_TAG)
JOB := core-${OTEL_TAG}
CLEAN_JOB := clean-${OTEL_TAG}

all: ${JOB}
clean: ${CLEAN_JOB}
.PHONY: all clean ${JOB} ${CLEAN_JOB}

${JOB}: core-%: Dockerfile
	docker buildx build \
	  --platform linux/arm64,linux/amd64 \
	  --build-arg OTEL_TAG=$* \
	  --tag dwolla/otel-collector:$*-SNAPSHOT \
	  .

${CLEAN_JOB}: clean-%:
	docker image rm --force dwolla/otel-collector:$*-SNAPSHOT
