{ fetchurl, stdenvNoCC, unzip }:

let
  release = {
    version = "2.24.0";
    assets = {
      "aarch64-darwin" = {
        suffix = "darwin_arm64";
        hash = "sha256-ZiTp3IXX7fAVES3LKz2mwrFoLTOWsPBVKHRnWdU1bJk=";
      };
      "x86_64-linux" = {
        suffix = "linux_amd64";
        hash = "sha256-M+VtrVxvvAV4/odmxrsZmTzUxs99tet3loiC4yYB7Ok=";
      };
    };
  };
  asset = release.assets.${stdenvNoCC.hostPlatform.system}
    or (throw "render-cli does not provide a binary for ${stdenvNoCC.hostPlatform.system}");
in
stdenvNoCC.mkDerivation {
  pname = "render-cli";
  version = release.version;
  src = fetchurl {
    url = "https://github.com/render-oss/cli/releases/download/v${release.version}/cli_${release.version}_${asset.suffix}.zip";
    hash = asset.hash;
  };
  nativeBuildInputs = [ unzip ];
  unpackPhase = "unzip $src";
  installPhase = ''
    install -Dm755 cli_v${release.version} $out/bin/render
  '';
}
