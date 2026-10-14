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

VENV := target/venv
CONFIGS := otel-config.yaml otel-agent-gateway.yaml otel-gateway.yaml otel-gateway-xray.yaml
.PHONY: check validate e2e

$(VENV)/bin/pytest: test/e2e/requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet -r test/e2e/requirements.txt
	touch $@

# Validate every config baked into the image (the ones that exist yet). Placeholder AWS settings: validate
# builds the components, and sigv4auth needs a region and resolvable credentials.
validate: core
	@for c in $(CONFIGS); do \
	  if [ -f $$c ]; then \
	    for regex in '\Afoo-batch\z' ''; do \
	      echo "validate $$c (batch regex: '$$regex')"; \
	      docker run --rm -e AWS_REGION=us-west-2 -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test -e DWOLLA_ENV=Test \
	        -e TAIL_SAMPLING_SPANS_PER_SEC=600 -e TAIL_SAMPLING_BASELINE_PERCENT=10 -e TAIL_SAMPLING_API_PERCENT=25 \
	        -e TAIL_SAMPLING_NEW_TRACES_PER_SEC=100 -e TAIL_SAMPLING_BATCH_PERCENT=0.5 \
	        $${regex:+-e TAIL_SAMPLING_BATCH_SERVICES_REGEX=$$regex} \
	        dwolla/otel-collector:latest validate --config=/etc/$$c || exit 1; \
	    done; \
	  fi; \
	done

e2e: core $(VENV)/bin/pytest
	$(VENV)/bin/pytest -v test/e2e

# The canonical check: build, validate every config, run the end-to-end tests.
check: validate e2e
