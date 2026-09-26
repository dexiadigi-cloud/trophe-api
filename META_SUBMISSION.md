# Trophe — Meta built-in connector submission sheet

Copy-paste reference for the submission form (submit from phone, same as Phos).

## Basics
- **Connector name:** Trophe
- **Tagline:** Food and nutrition data from USDA FoodData Central
- **Description:** Trophe is a read-only food and nutrition API built on USDA
  FoodData Central (public domain data). Search 2M+ foods, get full nutrient
  profiles, total a meal, compare two foods, and browse branded products by
  company. Free, no account required.
- **Base URL:** https://trophe-373637331608.us-west1.run.app
- **OpenAPI spec:** https://trophe-373637331608.us-west1.run.app/openapi.json
- **Auth:** API key in the `X-API-Key` header (already stored as the
  `custom.trophe` credential)
- **Privacy policy:** https://trophe-373637331608.us-west1.run.app/privacy
- **Terms of service:** https://trophe-373637331608.us-west1.run.app/terms

## Logo
Upload the attached file: `trophe-logo-icon-512.png`
(bowl-and-leaf mark, approved 2026-09-25; 512x512, ~126 KiB, under the
256 KiB icon limit; full-size master is `trophe-logo-official.png`)

## Disclaimer (for the listing)
Food and nutrition information is reference material only, not medical or
dietary advice. Branded food values are manufacturer-reported; check package
labels. USDA source data is public domain (CC0).

## Reviewer notes
- v1 is US-first (USDA FoodData Central, 2,013,644 foods).
- 7 endpoints: health, search, food, nutrients, meal, compare, brands.
- Read-only API; no user accounts, no data collection.

## Example prompts
1. How much protein is in 3 whole eggs?
2. Compare the calories and protein in whole milk vs skim milk.
3. What are the nutrients in 100g of raw chicken breast?
4. Add up the calories and protein for my breakfast: 2 eggs, a slice of toast, and a glass of orange juice.
5. Which has more fiber, an apple or a banana?
