terraform {
  # 1.1.5 is the provider's own floor; 1.16.0 is what this was built and applied against.
  required_version = ">= 1.1.5"

  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.5"
    }
  }
}

# Talks to the local daemon over the unix socket. There is no cloud here and the docs say so
# plainly (docs/platform.md): what is being demonstrated is that the stack is defined as code and
# is reproducibly tear-down-able, not that it is multi-region.
provider "docker" {}
