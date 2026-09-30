# posthog-workflows-action

Checks PostHog workflow YAML files on pull requests and applies them on the default branch.
It is a composite action with one bash script that needs only `curl` and `jq`, so nothing is installed on the runner.

This is an MVP for testing workflows as code. The `code_check` and `code_apply` endpoints it calls are still in development.

## Usage

Two jobs: `check` runs on pull requests with a read key, and `apply` runs on a push to the default branch with a write key.
The write key lives in a GitHub environment that only the default branch may use, so a pull request run never gets it.

```yaml
name: PostHog workflows

on:
  pull_request:
    paths: ['workflows/**']
  push:
    branches: [main]
    paths: ['workflows/**']

permissions:
  contents: read

concurrency:
  group: posthog-workflows-${{ github.head_ref || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  check:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: Silthus/posthog-workflows-action@028eaa17c6cd3dbb46f2a9d80b956571a0573976 # v0.1.1
        with:
          api-key: ${{ secrets.POSTHOG_API_KEY }}
          project-id: ${{ vars.POSTHOG_PROJECT_ID }}
          mode: check

  apply:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    environment: posthog-workflows
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: Silthus/posthog-workflows-action@028eaa17c6cd3dbb46f2a9d80b956571a0573976 # v0.1.1
        with:
          api-key: ${{ secrets.POSTHOG_API_KEY }}
          project-id: ${{ vars.POSTHOG_PROJECT_ID }}
          mode: apply
```

Change `main` if your default branch has another name.
Pin the action to a full commit SHA, as above, because the `apply` job gives it a write key. `v0` is a tag that moves with each release.
The `concurrency` group runs one job per branch at a time. A running apply is never canceled, and a queued one gives way to the newest push, which applies every file anyway.

## Setup

1. In PostHog, create two personal API keys limited to the project: one with `hog_flow:read` for checks, and one with `hog_flow:write` for applies.
2. In the repository, open Settings, Secrets and variables, Actions. Add the read key as the repository secret `POSTHOG_API_KEY`, and the project id as the repository variable `POSTHOG_PROJECT_ID`.
3. Open Settings, Environments, and create the environment `posthog-workflows`. Under deployment branches, allow only the default branch. Add the write key to it as the environment secret `POSTHOG_API_KEY`.

Both secrets have the same name and hold different keys. A job that declares `environment: posthog-workflows` reads the environment's value, and every other job reads the repository's value.
So the `apply` job gets the write key, and the `check` job gets the read key.
Create the environment before the first push to the default branch. If a workflow names an environment that does not exist, GitHub creates it without the branch limit.

A project secret API key (`phs_`) works once [PostHog/posthog#104202](https://github.com/PostHog/posthog/pull/104202) is deployed. Until then PostHog answers it with HTTP 401.

## What it does

- On a pull request each file is checked. Errors show as annotations on the file. The plan goes to the job summary: `create`, `update`, `stage` or `unchanged`, with the steps added, changed and removed.
- On a push to the default branch each file is applied, and each prints `created`, `updated` or `unchanged`. Any error fails the job.
- A request that gets no answer, HTTP 409 (a parallel apply of the same key), 429 or a 5xx is sent once more after two seconds.
- The host must use `https://`. Plain `http://` works only for `localhost` and `127.0.0.1`, so the key never travels unencrypted.
- When no file matches `files`, the action prints one line and succeeds, so deleting the last workflow file keeps the job green.
- When the key is empty, the action prints one line and succeeds. Pull requests from forks get no secrets, so they pass this way. In apply mode that line is a warning, because an empty key there usually means the environment secret is missing.
- Text from PostHog and file names are printed with workflow commands stopped, so a message cannot run a workflow command.

## Inputs

| Input        | Required | Default                  | Meaning                                                                                  |
| ------------ | -------- | ------------------------ | ---------------------------------------------------------------------------------------- |
| `api-key`    | yes      |                          | A personal API key with `hog_flow:read` to check, or `hog_flow:write` to apply           |
| `project-id` | yes      |                          | The PostHog project id                                                                   |
| `host`       | no       | `https://us.posthog.com` | The PostHog host, for example `https://eu.posthog.com`                                   |
| `files`      | no       | `workflows/*.yaml`       | One or more globs, separated by spaces or line breaks                                    |
| `mode`       | no       | `auto`                   | `auto` checks pull requests and applies pushes to the default branch; `check` or `apply` |

## Not yet

- No `source` (repository, path, commit) is sent with an apply until PostHog accepts it.
- No secrets in files. A value for a secret input is refused; set secrets in PostHog.
- No deletes. Removing a file leaves its workflow in PostHog.
