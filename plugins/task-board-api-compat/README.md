# task-board-api-compat

Compatibility service that lets `@linxin666/dsh-client-ui-task-board@0.3.6` run on the
current Typert-based DSH Host. It exposes only the legacy `apiProxy` methods used by the
task board and delegates them to the current Session, Workspace, Agent, and Preset services.

This plugin owns no UI and no task ledger. Remove it together with the task-board bundle.
