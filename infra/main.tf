terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = local.region
  # Fail fast if the active AWS profile is the wrong account, instead of
  # creating the role there.
  allowed_account_ids = [local.account_id]
}

locals {
  account_id  = "897837310411"
  region      = "us-east-1"
  github_repo = "twilio/rtc-cpp-tests"
  test_bucket = "cpp-test-artifacts"

  # This repo uses GitHub's immutable OIDC subject claims, so the `sub` prefix
  # carries the owner and repo IDs (twilio = 109142, rtc-cpp-tests = 1371610559),
  # not just their names: `repo:twilio@109142/rtc-cpp-tests@1371610559`. A token
  # with the name-only prefix never arrives, so matching on it fails every
  # AssumeRoleWithWebIdentity. The IDs also mean a repo deleted and recreated
  # under the same name can't assume this role. Current value:
  # `gh api repos/twilio/rtc-cpp-tests/actions/oidc/customization/sub`.
  github_sub_prefix = "repo:twilio@109142/rtc-cpp-tests@1371610559"

  # Only runs on `main` may assume this role. `main` is the only protected
  # branch, and repository_dispatch always runs there. Any branch would let
  # anyone who can push one (a collaborator, or anyone holding a token with
  # repo/workflow scope) run a modified workflow that reads and decrypts the
  # private test binaries, and this repo's Actions logs are public.
  #
  # The exact match also excludes fork PRs: they get an OIDC token, but its
  # `sub` is `<prefix>:pull_request`, which never matches.
  #
  # DO NOT widen this (to other branches, `refs/heads/*`, or `:*`) without a
  # security review. Any expansion (e.g. a protected GitHub environment) must
  # be an explicit, narrowly scoped alternative, never a wildcard.
  oidc_sub = "${local.github_sub_prefix}:ref:refs/heads/main"
}

# GitHub's OIDC provider is a per-AWS-account singleton. If Terraform errors
# with "no matching provider found", it hasn't been registered in this account
# yet — swap this `data` block for an `aws_iam_openid_connect_provider` resource
# (thumbprint 6938fd4d98bab03faadb97b34396831e3780aea1, client_id_list = ["sts.amazonaws.com"])
# and apply once, then switch back.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.oidc_sub]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "github" {
  name               = "rtc-cpp-tests-github-role"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  description        = "GitHub Actions OIDC role for ${local.github_repo}. Read-only on s3://${local.test_bucket} for pulling rtc-cpp test artifacts."
}

# The tarballs are encrypted with a customer-managed KMS key owned by rtc-cpp's
# infra/ (VBLOCKS-7414). That key must exist before this is applied: rtc-cpp
# applies its key first, then this, then switches the bucket default to the key.
data "aws_kms_alias" "test_artifacts" {
  name = "alias/${local.test_bucket}"
}

data "aws_iam_policy_document" "test_artifacts_read" {
  # Read-only on the packages/ prefix rtc-cpp uploads to. GetObjectVersion is
  # required for the dispatch path, which downloads with `--version-id`.
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["arn:aws:s3:::${local.test_bucket}/packages/*"]
  }
  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${local.test_bucket}"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["packages/*", "packages/"]
    }
  }
  # Decrypt the SSE-KMS tarballs. Only through S3 (kms:ViaService), so the role
  # can't use the key on anything except objects it can already GetObject.
  # Older SSE-S3 objects need no KMS permission and still read without it.
  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.test_artifacts.target_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "test_artifacts_read" {
  name   = "test-artifacts-read"
  role   = aws_iam_role.github.id
  policy = data.aws_iam_policy_document.test_artifacts_read.json
}

output "role_arn" {
  value       = aws_iam_role.github.arn
  description = "Set this as role-to-assume on the aws-actions/configure-aws-credentials step in the GitHub Actions workflow."
}
