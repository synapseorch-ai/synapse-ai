"""This process's telemetry state. Read on hot paths, so plain module flags."""

#: `setup()` has decided (on or off) for this process.
configured = False
#: Spans are being exported: engine spans are worth creating.
tracing = False
#: A meter provider is installed: recording a metric reaches something.
metering = False
