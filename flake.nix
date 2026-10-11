# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
# Modified 2026-10-11: raw capture build, independent C++ backend and regression tests.
{
  description = "FocalTech FT9362 raw capture development driver (no authentication)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          libfprint-focaltech0752 = pkgs.libfprint.overrideAttrs (oldAttrs: {
            pname = "libfprint-focaltech0752";

            postPatch = (oldAttrs.postPatch or "") + ''
              cp ${./driver/focaltech-0752.c} libfprint/drivers/focaltech0752.c
              cp -r ${./driver/algorithm} libfprint/drivers/algorithm
              cp ${./tests/test-focaltech-raw.c} tests/test-focaltech-raw.c
              cp ${./tests/test-ft9362-algorithm.c} tests/test-ft9362-algorithm.c
              cat >> tests/meson.build <<'EOF'
              raw_test = executable('test-focaltech-raw', 'test-focaltech-raw.c',
                dependencies: [libfprint_private_dep, focaltech_algo_dep], c_args: common_cflags)
              test('focaltech-raw', raw_test, suite: ['unit-tests'], env: envs)
              algorithm_test = executable('test-ft9362-algorithm', 'test-ft9362-algorithm.c',
                dependencies: focaltech_algo_dep)
              test('ft9362-algorithm', algorithm_test, suite: ['unit-tests'])
              EOF

              substituteInPlace libfprint/meson.build \
                --replace-fail "driver_sources = {" "driver_sources = {
                  'focaltech0752': files('drivers/focaltech0752.c')," \
                --replace-fail "libfprint_drivers = static_library(" "subdir('drivers/algorithm')
              libfprint_drivers = static_library(" \
                --replace-fail "    c_args: drivers_cflags,
                  dependencies: deps," "    c_args: drivers_cflags,
                  dependencies: [deps, focaltech_algo_dep],"
              substituteInPlace meson.build \
                --replace-fail "drivers_info = {" "drivers_info = {
                  'focaltech0752': {},"
            '';

            # Generate hwdb locally; upstream sync-udev-hwdb also fetches wiki data.
            postBuild = (oldAttrs.postBuild or "") + ''
              ninja libfprint/autosuspend.hwdb
              cp libfprint/autosuspend.hwdb ../data/autosuspend.hwdb
            '';

            postInstall = (oldAttrs.postInstall or "") + ''
              mkdir -p $out/lib/udev/rules.d
              cat > $out/lib/udev/rules.d/60-focaltech0752.rules << 'EOF'
              SUBSYSTEM=="usb", ATTRS{idVendor}=="2808", ATTRS{idProduct}=="0752", MODE="0664", GROUP="plugdev", TAG+="uaccess"
              EOF
            '';
          });
        in
        {
          libfprint = libfprint-focaltech0752;
          default = libfprint-focaltech0752;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          libfprint-focaltech0752 = self.packages.${system}.libfprint;
        in
        {
          default = pkgs.mkShell {
            nativeBuildInputs = [ pkgs.gobject-introspection ];
            buildInputs = with pkgs; [
              libfprint-focaltech0752
              libusb1
              glib
              gtk3
              librsvg
              pkg-config
              meson
              ninja
              gcc
              (python3.withPackages (p: [ p.pygobject3 ]))
            ];

            shellHook = ''
              export GI_TYPELIB_PATH="${libfprint-focaltech0752}/lib/girepository-1.0:${pkgs.lib.getLib pkgs.gusb}/lib/girepository-1.0:${pkgs.lib.getLib pkgs.json-glib}/lib/girepository-1.0:${pkgs.lib.getLib pkgs.glib}/lib/girepository-1.0''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
              echo "FT9362 raw capture development shell - verification is bypassed"
              echo "  python tools/capture.py       # live preview and recording"
              echo "  python tools/capture.py --cli # single-frame terminal capture"
            '';
          };
        }
      );
    };
}
