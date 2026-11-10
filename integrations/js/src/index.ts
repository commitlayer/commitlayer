/**
 * `@commitlayer/integrations`: CommitLayer for workflow engines.
 *
 * - `@commitlayer/integrations/temporal`: an activity interceptor that records a mapped activity as an action
 *   before it runs, and a `commitlayerVerify` activity that waits for the verdict.
 * - `@commitlayer/integrations/inngest`: `recordedStep` and `waitForVerdict` for Inngest functions.
 *
 * This root export holds what both share: the action body, path helpers, the verdict summary and the polling loop.
 */

export * from "./common.js";
