# frozen_string_literal: true

# Data repository for the ScenicSpots 旅遊景點 demonstration dictionary.
# The dictionary itself (reference-docs/examples/scenicspots.cddal) is the
# single source; `rake browser:build` renders it into data/ as browser
# JSON with unit references bound into the IEC 62720 units dictionary.
# The gem (opencdd-ruby) is the implementation and is used read-only
# via the sibling checkout (Gemfile path dependency).

require "json"
require "fileutils"

SLUG = "scenicspots"
TITLE = 'ScenicSpots 旅遊景點 (demonstration)'
TRANSLATIONS = ['zh-TW', 'zh-HK', 'zh-CN', 'ja', 'ko', 'fr', 'it', 'de', 'es', 'th', 'vi', 'pt', 'ar', 'tr', 'el', 'nl', 'pl', 'cs', 'hu']
DATA_DIR = File.expand_path("data", __dir__)
FIXTURE = File.expand_path("reference-docs/examples/scenicspots.cddal", __dir__)
UNITS_JSON = File.expand_path("units.json", __dir__)

def unit_index
  @unit_index ||= begin
    units = JSON.parse(File.read(UNITS_JSON))["units"]
    idx = {}
    units.each do |u|
      [u["preferred_name"], *Array(u["synonyms"])].each do |n|
        key = n.to_s.downcase.strip.tr("_", " ")
        idx[key] ||= u["irdi"] unless key.empty?
      end
    end
    idx
  end
end

def bind_iec_units!(json_path)
  entities = JSON.parse(File.read(json_path))
  unresolved = []
  entities.each do |e|
    u = e["unit"]
    next unless u.is_a?(String) && !u.empty? && !u.include?("#")
    irdi = unit_index[u.downcase.tr("_", " ")]
    if irdi
      e["unit"] = irdi
      e["unit_text"] = u.tr("_", " ")
    else
      unresolved << "#{e["code"]}: #{u}"
    end
  end
  abort "unresolved unit references:\n  #{unresolved.join("\n  ")}" unless unresolved.empty?
  File.write(json_path, JSON.pretty_generate(entities))
end

def count_entities(database)
  {
    class: database.classes.size,
    property: database.properties.size,
    value_list: database.value_lists.size,
    value_term: database.value_terms.size,
    unit: database.units.size,
    relation: database.relations.size,
    view_control: database.view_controls.size,
  }
end

desc "Build browser JSON from the fixture"
task "browser:build" do
  require "cdd"
  db = Cdd::Cddal.parse_file(FIXTURE)
  json = Cdd::Exporters::Json.new.to_json(db)
  out_dir = File.join(DATA_DIR, SLUG)
  FileUtils.mkdir_p(out_dir)
  File.write(File.join(out_dir, "database.json"), json)
  bind_iec_units!(File.join(out_dir, "database.json"))
  registry = { "dictionaries" => [{
    "slug" => SLUG,
    "parcelId" => SLUG.upcase,
    "title" => TITLE,
    "sourceLanguage" => "en",
    "translationLanguages" => TRANSLATIONS,
    "counts" => count_entities(db),
    "metaClassIrdis" => [],
  }] }
  File.write(File.join(DATA_DIR, "index.json"), JSON.pretty_generate(registry))
  puts "Wrote #{SLUG} → #{out_dir}"
end

task default: "browser:build"

HARVEST_BEGIN = "# ==== BEGIN DBPEDIA BULK REGISTRY (generated - do not hand-edit) ===="
HARVEST_END = "# ==== END DBPEDIA BULK REGISTRY ===="

desc "Harvest DBpedia POIs, verify each against Wikipedia, enrich from " \
     "Wikidata, and splice into the fixture. Optional subset: " \
     "rake browser:harvest_poi[Taiwan,Japan]"
task "browser:harvest_poi", [:countries] do |_t, args|
  cmd = %w[python3 harvest/dbpedia_poi.py --fixture] << FIXTURE
  cmd += ["--countries", args[:countries]] if args[:countries]
  sh *cmd
  bulk = File.read("harvest/out/poi_bulk.cddal")
  text = File.read(FIXTURE)
  marked = /#{Regexp.escape(HARVEST_BEGIN)}.*?#{Regexp.escape(HARVEST_END)}\n/m
  abort "no bulk registry section found in #{FIXTURE}" unless marked.match?(text)
  File.write(FIXTURE, text.sub(marked) { bulk })
  puts "Spliced bulk registry into #{FIXTURE}"
  Rake::Task["browser:build"].invoke
end
