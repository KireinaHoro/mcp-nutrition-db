{ runCommand, fetchurl, writeText, python313, src }:
let
  sources = [
    {
      data_type = "Foundation";
      release = "2026-04-30";
      url = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_json_2026-04-30.zip";
      sha256 = "186e988ec542e913f51ef62b86a47758e8cdd0d1dc3889e7b055581f3c09c77a";
    }
    {
      data_type = "SR Legacy";
      release = "2018-04";
      url = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_json_2018-04.zip";
      sha256 = "0fe8ae486a2c8eb42cb96413f058deb51863a46c8fb8eeb4b1fb45006dd338ef";
    }
    {
      data_type = "Survey (FNDDS)";
      release = "2024-10-31";
      url = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_survey_food_json_2024-10-31.zip";
      sha256 = "dfb06ae7ddc397ccd570b91c14b75438ab2ba39f64f22d321f61d4a52a77f3eb";
    }
  ];
  manifest = writeText "usda-sources.json" (builtins.toJSON (map
    (source: source // { path = fetchurl { inherit (source) url sha256; }; }) sources));
  python = python313.withPackages (ps: [ ps.pydantic ]);
in
runCommand "usda-reference-2026-04" { nativeBuildInputs = [ python ]; } ''
  mkdir -p "$out/share/mcp-nutrition-db"
  export PYTHONPATH=${src}/src
  python -m mcp_nutrition_db.usda_dataset \
    --manifest ${manifest} \
    --output "$out/share/mcp-nutrition-db/usda.sqlite3" \
    > "$out/share/mcp-nutrition-db/usda-manifest.json"
''
