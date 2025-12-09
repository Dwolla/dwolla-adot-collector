package linkprocessor

import "go.opentelemetry.io/collector/component"

type Config struct {
	// AttributeName is the name of the attribute to set. Defaults to "linked_trace_ids".
	AttributeName string `mapstructure:"attribute_name"`
}

var _ component.Config = (*Config)(nil)

func (c *Config) Validate() error {
	return nil
}
