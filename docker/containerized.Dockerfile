# Image the containerized (network) test suites run in.
#
# rtc-cpp's CircleCI built its test image on the private ECR toolchain image,
# which this public repo can't pull. These suites only need a glibc new enough
# for the binaries (they require GLIBC_2.38; 24.04 has 2.39) plus the tools
# they shell out to:
#   - ip, tc (iproute2) and iptables: common/test/support/net_utils.cpp, and
#     the runner's per-test network reset (iptables-save/-restore)
#   - tshark: voice/test/containerized/dscp_test.cpp captures packets with it
#     (the toolchain image installed it too)
#   - the docker CLI, talking to the host daemon over the mounted socket:
#     common/test/support/net_handoff_utils.cpp creates/connects networks.
#     The toolchain image had no docker CLI, so those tests couldn't work in
#     CircleCI either.
#   - python3 for scripts/run_gtest_isolated.py.
#   - libx11-6: every linux test binary links libX11.so.6.
# The test binaries, media and runner script are bind-mounted at run time, not
# baked in, so the image is the same for every suite.
FROM ubuntu:24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates docker.io iproute2 iptables libx11-6 python3 tshark \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/twilio
