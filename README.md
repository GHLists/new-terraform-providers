# New Terraform providers

Hourly lists of providers newly published to the
[Terraform Registry](https://registry.terraform.io/). The registry's website
API caps its provider listing, so the full provider list is taken from the
[OpenTofu registry mirror](https://github.com/opentofu/registry) and diffed
against the previous run; every unseen provider is verified through the
registry detail endpoint, whose `published_at` timestamp decides whether the
provider was published inside the window.
A GitHub Actions workflow runs every hour, fetches the providers published
since the previous list and commits one CSV per run to [`data/`](data/), e.g.
[`data/new-terraform-providers-<timestamp>.csv`](data/).

Read the latest list below.

## Latest list — 2026-11-13 15:22 UTC

New providers published between 2026-11-13 14:22 UTC and 2026-11-13 15:22 UTC.

[Full CSV](data/new-terraform-providers-2026-11-13T15-22-06Z.csv)

| Published (UTC) | Provider | Namespace | Version | Description |
| :-------------- | :------- | :-------- | :------ | :---------- |
| 2026-11-13 14:40:13 | [glueops/proxmox](https://registry.terraform.io/providers/glueops/proxmox) | glueops | 0.1.0 |  |

## Data source

Data comes from the Terraform Registry and the OpenTofu registry mirror.
Provider metadata is provided by the providers' maintainers. This project is
not affiliated with or endorsed by HashiCorp or the OpenTofu project.
