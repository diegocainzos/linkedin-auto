# LinkedIn search filters

`jobs.py run` builds a LinkedIn job search URL. The **Easy Apply** filter (`f_AL=true`) is always on.
Set your usual filters in `search.toml` (same names as the flags, with `_` instead of `-`, e.g. `job_type`).
Flags override them for a single run. Use `--dry-run` to print the URL without starting the agent,
and open it in your browser to check it.

```bash
uv run jobs.py run "python developer" --location Spain \
  --posted week --experience entry,associate --workplace remote,hybrid \
  --job-type full-time --sort recent --dry-run
```

Flags that accept several values take a comma list (`--workplace remote,hybrid`). LinkedIn treats values
within one filter as OR, and different filters as AND.

## Built-in flags

| Flag | Values | URL param | LinkedIn UI name |
|---|---|---|---|
| `position` (required) | free text | `keywords` | Search box |
| `--location` | free text: country, city, region, "Remote" | `location` | Location box |
| `--posted` | `24h`, `week`, `month` | `f_TPR` = `r86400`, `r604800`, `r2592000` | Date posted |
| `--experience` | `internship`, `entry`, `associate`, `mid-senior`, `director`, `executive` | `f_E` = `1`…`6` | Experience level |
| `--workplace` | `onsite`, `remote`, `hybrid` | `f_WT` = `1`, `2`, `3` | Remote |
| `--job-type` | `full-time`, `part-time`, `contract`, `temporary`, `internship`, `volunteer`, `other` | `f_JT` = `F`, `P`, `C`, `T`, `I`, `V`, `O` | Job type |
| `--sort` | `recent`, `relevant` | `sortBy` = `DD`, `R` | Sort by |
| `--distance` | miles: `0`, `5`, `10`, `25`, `50`, `100` | `distance` | Distance |
| `--under-10-applicants` | flag | `f_JIYN=true` | Under 10 applicants |

`--posted` also accepts any custom window as a raw filter: `--filter f_TPR=r3600` means the past hour
(`r` + seconds).

## Raw filters: `--filter KEY=VALUE`

Everything else in LinkedIn's filter panel uses numeric IDs. Get them by setting the filter by hand in
LinkedIn, copying the value from the address bar, and passing it with `--filter` (repeatable):

| Filter | URL param | Example |
|---|---|---|
| Company | `f_C` | `--filter f_C=1441,1035` (IDs are in the URL after picking companies) |
| Industry | `f_I` | `--filter f_I=4` (4 = Software Development) |
| Job function | `f_F` | `--filter f_F=it,eng` |
| Title | `f_T` | `--filter f_T=9,25201` |
| Exact location | `geoId` | `--filter geoId=105646813` (Spain), more reliable than the `--location` text |
| Specific cities | `f_PP` | `--filter f_PP=106967730` (city geoIds) |
| In your network | `f_EA` | `--filter f_EA=true` |
| Salary (mostly US) | `f_SB2` | `--filter f_SB2=5` (buckets 1-9 = $40k+ … $200k+) |
| Has verified hiring info | `f_VJ` | `--filter f_VJ=true` |

## How sure are these codes?

- Checked against LinkedIn on 2026-09-27: `f_AL`, `f_TPR` (all three values) and `f_EA` change the
  results of the logged-out job search, and the Date posted and Company options match the labels above.
- LinkedIn only applies `f_E`, `f_WT`, `f_JT`, `f_JIYN` and `sortBy` for **logged-in** users, so the
  logged-out check couldn't confirm them. These are LinkedIn's long-standing codes, and on the first
  results page the agent checks that the filter pills show them as active. If LinkedIn renames a code,
  the agent reports it, and the fix is one line in `FILTERS` in `jobs.py`.
- The `f_I`, `f_F`, `f_T`, `f_SB2` and `f_VJ` examples are common values, not verified. Copying from your
  address bar is the source of truth.
