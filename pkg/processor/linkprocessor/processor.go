package linkprocessor

import (
	"context"
	"encoding/hex"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor"
	"go.uber.org/zap"
)

type linkProcessor struct {
	logger        *zap.Logger
	nextConsumer  consumer.Traces
	attributeName string
}

func newLinkProcessor(cfg *Config, logger *zap.Logger, next consumer.Traces) (processor.Traces, error) {
	return &linkProcessor{
		logger:        logger,
		nextConsumer:  next,
		attributeName: cfg.AttributeName,
	}, nil
}

func (p *linkProcessor) ConsumeTraces(ctx context.Context, td ptrace.Traces) error {
	rss := td.ResourceSpans()
	for i := 0; i < rss.Len(); i++ {
		rs := rss.At(i)
		ilss := rs.ScopeSpans()
		for j := 0; j < ilss.Len(); j++ {
			ils := ilss.At(j)
			spans := ils.Spans()
			for k := 0; k < spans.Len(); k++ {
				span := spans.At(k)
				p.processSpan(span)
			}
		}
	}
	return p.nextConsumer.ConsumeTraces(ctx, td)
}

func (p *linkProcessor) processSpan(span ptrace.Span) {
	links := span.Links()
	if links.Len() == 0 {
		return
	}

	// Create a new slice in the attributes map directly
	// pdata v1.x uses PutEmptySlice which returns the slice to be filled
	traceIDs := span.Attributes().PutEmptySlice(p.attributeName)
	// Ensure capacity
	traceIDs.EnsureCapacity(links.Len())
	
	for i := 0; i < links.Len(); i++ {
		link := links.At(i)
		traceID := link.TraceID()
		// Convert [16]byte TraceID to hex string
		hexID := hex.EncodeToString(traceID[:])
		traceIDs.AppendEmpty().SetStr(hexID)
	}
}

func (p *linkProcessor) Capabilities() consumer.Capabilities {
	return consumer.Capabilities{MutatesData: true}
}

func (p *linkProcessor) Start(_ context.Context, _ component.Host) error {
	return nil
}

func (p *linkProcessor) Shutdown(_ context.Context) error {
	return nil
}
