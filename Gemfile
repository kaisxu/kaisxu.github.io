source "https://rubygems.org"

# Jekyll core + plugins used by Chirpy.
# Theme is installed as a local gem so its _sass/ is on the Sass load path
# (remote_theme doesn't do this reliably for Chirpy).
gem "jekyll", "~> 4.3"
gem "jekyll-theme-chirpy", "~> 7.6"
gem "jekyll-feed", "~> 0.17"
gem "jekyll-seo-tag", "~> 2.8"
gem "jekyll-sitemap", "~> 1.4"
gem "jekyll-paginate", "~> 1.1"
gem "jekyll-archives", "~> 2.2"
gem "jekyll-include-cache", "~> 0.2"

# Multilingual support. Needs >= 1.11.0: earlier versions built an unescaped
# regex out of `exclude:` and died with `RegexpError` (or hung in a near-
# infinite gsub) on Chirpy's glob entries — untra/polyglot#204, #260.
# Requires Ruby >= 3.2 as of 1.13; the Actions workflow pins 3.2.
gem "jekyll-polyglot", "~> 1.11"

# Windows-only gems (harmless on Linux)
platforms :windows, :jruby do
  gem "tzinfo", ">= 1", "< 3"
  gem "tzinfo-data"
end

gem "wdm", "~> 0.2.0", :platforms => [:windows]