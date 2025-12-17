# Default to loading into local docker daemon. Override with --push for remote.
OUTPUT ?= --load
# Default to empty to let Docker pick the local platform. Override for multi-arch.
PLATFORM ?= 

all: core
clean: clean-core
.PHONY: all clean core clean-core

core: Dockerfile
	docker buildx build \
	  $(OUTPUT) \
	  $(if $(PLATFORM),--platform $(PLATFORM),) \
	  --tag dwolla/otel-collector:latest \
	  .

clean-core:
	-docker image rm --force dwolla/otel-collector:latest
