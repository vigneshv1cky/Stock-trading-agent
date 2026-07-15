# AlphaDesk infrastructure — GCP (e2-medium on trial credit; free-tier-region)
#
# Usage:
#   gcloud auth application-default login          # once, browser OAuth
#   cd alphadesk/deploy/terraform
#   terraform init
#   terraform apply -var project_id=<PROJECT_ID>
#   → outputs the VM's external IP; then deploy per ../README.md section 2
#
# Day-90 downsize to the permanent free tier: change machine_type to
# "e2-micro" and re-apply (in-place resize; add MAX_CONCURRENT_WORKFLOWS=1
# + let setup.sh's swap guard handle the rest).

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

variable "project_id" {
  description = "GCP project ID (gcloud projects list)"
  type        = string
}

variable "region" {
  description = "Must be a free-tier region: us-west1, us-central1, us-east1"
  type        = string
  default     = "us-east1"
}

variable "zone" {
  type    = string
  default = "us-east1-b"
}

variable "machine_type" {
  description = "e2-medium on trial credit; e2-micro for permanent free tier"
  type        = string
  default     = "e2-medium"
}

variable "ssh_public_key_file" {
  description = "Path to the SSH public key granted access as user 'ubuntu'"
  type        = string
  default     = "~/.ssh/id_rsa.pub"
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Fresh projects need the Compute API switched on
resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_firewall" "alphadesk_dashboard" {
  name    = "allow-alphadesk"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["8000"]
  }

  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]

  depends_on = [google_project_service.compute]
}

resource "google_compute_instance" "alphadesk" {
  name         = "alphadesk"
  machine_type = var.machine_type

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 30
      type  = "pd-standard" # free-tier-compatible; NOT the default pd-balanced
    }
  }

  network_interface {
    network = "default"
    access_config {} # ephemeral external IP
  }

  metadata = {
    ssh-keys = "ubuntu:${trimspace(file(pathexpand(var.ssh_public_key_file)))}"
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  depends_on = [google_project_service.compute]
}

output "external_ip" {
  value       = google_compute_instance.alphadesk.network_interface[0].access_config[0].nat_ip
  description = "SSH: ssh ubuntu@<this>; dashboard: http://<this>:8000"
}
