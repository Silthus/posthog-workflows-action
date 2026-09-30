# posthog-workflows-action

Checks PostHog workflow YAML files on pull requests and applies them on the default branch.
It is a composite action with one bash script that needs only `curl` and `jq`, so nothing is installed on the runner.

This is an MVP for testing workflows as code. The `code_check` and `code_apply` endpoints it calls are still in development.

## Usage

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
jobs:
  workflows:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: Silthus/posthog-workflows-action@v0
        with:
          api-key: ${{ secrets.POSTHOG_API_KEY }}
          project-id: ${{ vars.POSTHOG_PROJECT_ID }}
```

On a pull request each file is checked: errors show as annotations on the file, and the plan (created, updated or unchanged, with the steps added, changed and removed) goes to the job summary.
On a push to the default branch each file is applied. Any error fails the job.

## Inputs

| Input        | Required | Default                  | Meaning                                                                                  |
| ------------ | -------- | ------------------------ | ---------------------------------------------------------------------------------------- |
| `api-key`    | yes      |                          | A project secret API key, or a personal API key with `hog_flow:read` and `hog_flow:write` |
| `project-id` | yes      |                          | The PostHog project id                                                                   |
| `host`       | no       | `https://us.posthog.com` | The PostHog host, for example `https://eu.posthog.com`                                   |
| `files`      | no       | `workflows/*.yaml`       | One or more space-separated globs                                                        |
| `mode`       | no       | `auto`                   | `auto` checks pull requests and applies pushes to the default branch; `check` or `apply` |

## Setup

1. In PostHog, create a project secret API key with the `hog_flow:read` and `hog_flow:write` scopes.
2. In the repository, add it as the secret `POSTHOG_API_KEY` (Settings, Secrets and variables, Actions, Secrets).
3. Add the project id as the variable `POSTHOG_PROJECT_ID` (same page, Variables).

To keep the write key away from pull request runs, put it in a GitHub environment that only the default branch may use.

When the key is empty the action prints one line and succeeds. Pull requests from forks get no secrets, so they pass this way.

## Not yet

- No `source` (repository, path, commit) is sent with an apply until PostHog accepts it.
- No secrets in files. A value for a secret input is refused; set secrets in PostHog.
- No deletes. Removing a file leaves its workflow in PostHog.
