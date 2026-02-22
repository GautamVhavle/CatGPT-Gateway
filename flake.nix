{
  description = "CatGPT browser-automation gateway";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
        };
        lib = pkgs.lib;

        src = ./.;

        patchrightVersion = "1.58.0";
        patchrightChromiumRevision = "1208";
        patchrightChromiumVersion = "145.0.7632.6";

        fontconfigFile = pkgs.makeFontsConf {
          fontDirectories = [
            pkgs.dejavu_fonts
            pkgs.liberation_ttf
            pkgs.noto-fonts
            pkgs.noto-fonts-cjk-sans
            pkgs.noto-fonts-color-emoji
          ];
        };

        patchrightChromium = pkgs.stdenv.mkDerivation {
          pname = "patchright-chromium";
          version = patchrightChromiumRevision;

          src = pkgs.fetchzip {
            url = "https://cdn.playwright.dev/chrome-for-testing-public/${patchrightChromiumVersion}/linux64/chrome-linux64.zip";
            hash = "sha256-akvAXdfBKdjDQBnWTDX0WbmP+niXthXlyB9feeq8kyw=";
            stripRoot = false;
          };

          nativeBuildInputs = [
            pkgs.autoPatchelfHook
            pkgs.makeWrapper
            pkgs.patchelf
          ];

          buildInputs = [
            pkgs.alsa-lib
            pkgs.at-spi2-atk
            pkgs.atk
            pkgs.cairo
            pkgs.cups
            pkgs.dbus
            pkgs.expat
            pkgs.glib
            pkgs.gobject-introspection
            pkgs.libdrm
            pkgs.libgbm
            pkgs.libxkbcommon
            pkgs.nspr
            pkgs.nss
            pkgs.pango
            pkgs.stdenv.cc.cc.lib
            pkgs.systemd
            pkgs.libx11
            pkgs.libxcomposite
            pkgs.libxdamage
            pkgs.libxext
            pkgs.libxfixes
            pkgs.libxi
            pkgs.libxrandr
            pkgs.libxrender
            pkgs.libxtst
            pkgs.libxcb
            pkgs.libxshmfence
          ];

          installPhase = ''
            runHook preInstall

            mkdir -p "$out/chrome-linux64"
            cp -R chrome-linux64/. "$out/chrome-linux64"
            chmod -R u+w "$out/chrome-linux64"
            touch "$out/INSTALLATION_COMPLETE"

            wrapProgram "$out/chrome-linux64/chrome" \
              --set-default SSL_CERT_FILE "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt" \
              --set-default FONTCONFIG_FILE "${fontconfigFile}"

            runHook postInstall
          '';

          appendRunpaths = lib.makeLibraryPath [
            pkgs.libGL
            pkgs.pciutils
            pkgs.vulkan-loader
          ];

          postFixup = ''
            if [ -e "$out/chrome-linux64/libvulkan.so.1" ]; then
              rm "$out/chrome-linux64/libvulkan.so.1"
              ln -s "${lib.getLib pkgs.vulkan-loader}/lib/libvulkan.so.1" "$out/chrome-linux64/libvulkan.so.1"
            fi
          '';
        };

        patchrightChromiumHeadlessShell = pkgs.stdenv.mkDerivation {
          pname = "patchright-chromium-headless-shell";
          version = patchrightChromiumRevision;

          src = pkgs.fetchzip {
            url = "https://cdn.playwright.dev/chrome-for-testing-public/${patchrightChromiumVersion}/linux64/chrome-headless-shell-linux64.zip";
            hash = "sha256-/xskLzTc9tTZmu1lwkMpjV3QV7XjP92D/7zRcFuVWT8=";
            stripRoot = false;
          };

          nativeBuildInputs = [
            pkgs.autoPatchelfHook
            pkgs.makeWrapper
            pkgs.patchelf
          ];

          buildInputs = [
            pkgs.alsa-lib
            pkgs.at-spi2-atk
            pkgs.atk
            pkgs.cairo
            pkgs.cups
            pkgs.dbus
            pkgs.expat
            pkgs.glib
            pkgs.gobject-introspection
            pkgs.libdrm
            pkgs.libgbm
            pkgs.libxkbcommon
            pkgs.nspr
            pkgs.nss
            pkgs.pango
            pkgs.stdenv.cc.cc.lib
            pkgs.systemd
            pkgs.libx11
            pkgs.libxcomposite
            pkgs.libxdamage
            pkgs.libxext
            pkgs.libxfixes
            pkgs.libxi
            pkgs.libxrandr
            pkgs.libxrender
            pkgs.libxtst
            pkgs.libxcb
            pkgs.libxshmfence
          ];

          installPhase = ''
            runHook preInstall

            mkdir -p "$out/chrome-headless-shell-linux64"
            cp -R chrome-headless-shell-linux64/. "$out/chrome-headless-shell-linux64"
            chmod -R u+w "$out/chrome-headless-shell-linux64"
            touch "$out/INSTALLATION_COMPLETE"

            wrapProgram "$out/chrome-headless-shell-linux64/chrome-headless-shell" \
              --set-default SSL_CERT_FILE "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt" \
              --set-default FONTCONFIG_FILE "${fontconfigFile}"

            runHook postInstall
          '';

          appendRunpaths = lib.makeLibraryPath [
            pkgs.libGL
            pkgs.pciutils
            pkgs.vulkan-loader
          ];
        };

        patchrightBrowsers = pkgs.linkFarm "patchright-browsers" [
          {
            name = "chromium-${patchrightChromiumRevision}";
            path = patchrightChromium;
          }
          {
            name = "chromium_headless_shell-${patchrightChromiumRevision}";
            path = patchrightChromiumHeadlessShell;
          }
        ];

        python = pkgs.python311.override {
          packageOverrides = pythonSelf: pythonSuper: {
            patchright = pythonSelf.buildPythonPackage rec {
              pname = "patchright";
              version = patchrightVersion;
              format = "wheel";

              src = pkgs.fetchPypi {
                inherit pname version format;
                dist = "py3";
                python = "py3";
                platform = "manylinux1_x86_64";
                hash = "sha256-gyvuL+SM+dwHuzsPDQXu6SMgPzSM2YsUwsUV7s4yZzQ=";
              };

              propagatedBuildInputs = [
                pythonSelf.greenlet
                pythonSelf.pyee
              ];

              doCheck = false;
              pythonImportsCheck = [ "patchright" ];

              meta = {
                description = "Undetected Python version of the Playwright automation library";
                homepage = "https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python";
                license = lib.licenses.asl20;
              };
            };
          };
        };

        pythonEnv = python.withPackages (ps: [
          ps.fastapi
          ps.uvicorn
          ps.pydantic
          ps."python-dotenv"
          ps.patchright
          ps."playwright-stealth"
          ps.textual
          ps.typer
          ps.rich
        ]);

        mkCatgptScript =
          {
            name,
            command,
          }:
          pkgs.writeShellApplication {
            inherit name;
            runtimeInputs = [
              pythonEnv
              pkgs.nodejs
            ];
            text = ''
              export PLAYWRIGHT_BROWSERS_PATH="${patchrightBrowsers}"
              export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
              export PLAYWRIGHT_SKIP_BROWSER_GC=1
              export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
              export PLAYWRIGHT_NODEJS_PATH="${pkgs.nodejs}/bin/node"

              state_home="''${XDG_STATE_HOME:-$HOME/.local/state}"

              if [ ! -f .env ]; then
                export BROWSER_DATA_DIR="''${BROWSER_DATA_DIR:-$state_home/catgpt/browser_data}"
                export LOG_DIR="''${LOG_DIR:-$state_home/catgpt/logs}"
                export IMAGES_DIR="''${IMAGES_DIR:-$state_home/catgpt/downloads/images}"
              fi

              mkdir -p "''${BROWSER_DATA_DIR:-$state_home/catgpt/browser_data}" \
                       "''${LOG_DIR:-$state_home/catgpt/logs}" \
                       "''${IMAGES_DIR:-$state_home/catgpt/downloads/images}"

               export PYTHONPATH="${src}:''${PYTHONPATH:-}"

               exec python ${command}
            '';
          };

        proxyScript = mkCatgptScript {
          name = "catgpt-proxy";
          command = "-m src.api.server";
        };

        loginScript = mkCatgptScript {
          name = "catgpt-login";
          command = "${src}/scripts/first_login.py";
        };

        tuiScript = mkCatgptScript {
          name = "catgpt-tui";
          command = "-m src.cli.app";
        };
      in
      {
        packages = {
          default = proxyScript;
          proxy = proxyScript;
          login = loginScript;
          tui = tuiScript;
          python-env = pythonEnv;
          patchright-browsers = patchrightBrowsers;
        };

        apps = {
          default = {
            type = "app";
            program = "${proxyScript}/bin/catgpt-proxy";
            meta.description = "Run CatGPT FastAPI proxy";
          };
          proxy = {
            type = "app";
            program = "${proxyScript}/bin/catgpt-proxy";
            meta.description = "Run CatGPT FastAPI proxy";
          };
          login = {
            type = "app";
            program = "${loginScript}/bin/catgpt-login";
            meta.description = "Run one-time ChatGPT login flow";
          };
          tui = {
            type = "app";
            program = "${tuiScript}/bin/catgpt-tui";
            meta.description = "Run CatGPT terminal UI";
          };
        };

        checks.imports =
          pkgs.runCommand "catgpt-import-check"
            {
              nativeBuildInputs = [
                pythonEnv
                pkgs.nodejs
              ];
              PLAYWRIGHT_BROWSERS_PATH = patchrightBrowsers;
              PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
              PLAYWRIGHT_SKIP_BROWSER_GC = "1";
              PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
              PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";
              PYTHONPATH = src;
            }
            ''
              export BROWSER_DATA_DIR="$TMPDIR/browser_data"
              export LOG_DIR="$TMPDIR/logs"
              export IMAGES_DIR="$TMPDIR/images"
              mkdir -p "$BROWSER_DATA_DIR" "$LOG_DIR" "$IMAGES_DIR"

              python - <<'PY'
              import patchright
              import playwright_stealth
              import src.api.server
              PY
              touch "$out"
            '';

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.nodejs
          ];

          PLAYWRIGHT_BROWSERS_PATH = patchrightBrowsers;
          PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
          PLAYWRIGHT_SKIP_BROWSER_GC = "1";
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
          PLAYWRIGHT_NODEJS_PATH = "${pkgs.nodejs}/bin/node";

          shellHook = ''
            if [ ! -f .env ]; then
              export BROWSER_DATA_DIR="''${BROWSER_DATA_DIR:-$PWD/browser_data}"
              export LOG_DIR="''${LOG_DIR:-$PWD/logs}"
              export IMAGES_DIR="''${IMAGES_DIR:-$PWD/downloads/images}"
            fi

            mkdir -p "''${BROWSER_DATA_DIR:-$PWD/browser_data}" \
                     "''${LOG_DIR:-$PWD/logs}" \
                     "''${IMAGES_DIR:-$PWD/downloads/images}"
          '';
        };

        formatter = pkgs.nixfmt;
      }
    );
}
