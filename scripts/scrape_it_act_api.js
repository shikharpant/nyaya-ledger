// ==========================================================================
// Nyaya Ledger — Income-tax Act 1961 Bulk API Scraper (rate-limited)
// ==========================================================================
//
// USAGE (Brave/Chrome Console on incometaxindia.gov.in/income-tax-act):
//
//   1. F12 → Console → type: allow pasting → Enter
//   2. Paste this entire script → Enter
//   3. Wait ~30 seconds
//   4. JSON auto-downloads
//
// Save to: data/Law/IT_ACT/income_tax_act_1961.json
// Then run: python3 scripts/ingest_it_act.py
// ==========================================================================

(async function() {
    var sections = [];
    var pageSize = 100;
    var totalSections = 935;
    var totalPages = Math.ceil(totalSections / pageSize);

    var searchBody = {
        "attributes": {
            "search.empty.search": true,
            "search.experiences.blueprint.external.reference.code": "ACT_SECTIONS_BP_ERC",
            "search.experiences.act_id": 4209131,
            "search.experiences.year_id": 13590315,
            "search.experiences.free_text": ""
        }
    };

    function sleep(ms) {
        return new Promise(function(resolve) { setTimeout(resolve, ms); });
    }

    console.log('[IT-Scraper] Starting... ' + totalPages + ' pages at ' + pageSize + '/page');

    for (var page = 1; page <= totalPages; page++) {
        try {
            var url = '/o/search/v1.0/search?nestedFields=embedded&page=' + page + '&pageSize=' + pageSize + '&restrictFields=embedded.actions%2Cembedded.creator';
            var resp = await fetch(url, {
                method: 'POST',
                headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
                body: JSON.stringify(searchBody)
            });

            if (!resp.ok) {
                console.log('[IT-Scraper] Page ' + page + ': HTTP ' + resp.status + ', waiting 5s before retry...');
                await sleep(5000);
                resp = await fetch(url, {
                    method: 'POST',
                    headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
                    body: JSON.stringify(searchBody)
                });
            }

            if (!resp.ok) {
                console.log('[IT-Scraper] Page ' + page + ': HTTP ' + resp.status + ' on retry, skipping');
                continue;
            }

            var data = await resp.json();
            var items = data.items || [];

            for (var i = 0; i < items.length; i++) {
                var item = items[i];
                var fields = {};
                if (item.embedded && item.embedded.contentFields) {
                    for (var f = 0; f < item.embedded.contentFields.length; f++) {
                        var cf = item.embedded.contentFields[f];
                        if (cf.contentFieldValue) {
                            fields[cf.name] = cf.contentFieldValue.data || '';
                        }
                    }
                }

                var sectionNumber = fields.sectionNumber || '';
                var description = fields.sectionShortDescription || '';
                var htmlContent = '';

                if (item.embedded && item.embedded.contentFields) {
                    for (var f = 0; f < item.embedded.contentFields.length; f++) {
                        var cf = item.embedded.contentFields[f];
                        var val = cf.contentFieldValue ? (cf.contentFieldValue.data || '') : '';
                        if (val.indexOf('<!DOCTYPE') >= 0 || val.indexOf('<html') >= 0) {
                            htmlContent = val;
                            break;
                        }
                    }
                }

                var plainText = '';
                if (htmlContent) {
                    var div = document.createElement('div');
                    div.innerHTML = htmlContent;
                    var scripts = div.querySelectorAll('script, style, noscript');
                    for (var s = 0; s < scripts.length; s++) scripts[s].remove();
                    plainText = div.textContent.trim();
                }

                sections.push({
                    section_number: sectionNumber,
                    description: description,
                    html_content: htmlContent,
                    full_text: plainText,
                    cms_id: fields.sectionCMSID || '',
                    upload_date: fields.uploadDate || '',
                });
            }

            console.log('[IT-Scraper] Page ' + page + '/' + totalPages + ': +' + items.length + ' (total: ' + sections.length + ')');

            if (page < totalPages) {
                var delay = (page % 3 === 0) ? 3000 : 2000;
                await sleep(delay);
            }

        } catch(e) {
            console.log('[IT-Scraper] Page ' + page + ': error: ' + e.message + ', waiting 5s...');
            await sleep(5000);
        }
    }

    // Deduplicate by section number (keep last = most recent)
    var seen = {};
    var unique = [];
    for (var i = sections.length - 1; i >= 0; i--) {
        var num = sections[i].section_number;
        if (!seen[num]) {
            seen[num] = true;
            unique.unshift(sections[i]);
        }
    }

    // Sort by section number
    unique.sort(function(a, b) {
        var an = a.section_number;
        var bn = b.section_number;
        var am = an.match(/^(\d+)(.*)/);
        var bm = bn.match(/^(\d+)(.*)/);
        if (am && bm) {
            var numDiff = parseInt(am[1]) - parseInt(bm[1]);
            if (numDiff !== 0) return numDiff;
            return am[2].localeCompare(bm[2]);
        }
        return an.localeCompare(bn);
    });

    var output = {
        source: 'https://www.incometaxindia.gov.in/income-tax-act',
        act: 'Income-tax Act, 1961',
        total_sections: unique.length,
        scraped_at: new Date().toISOString(),
        sections: unique
    };

    window.__IT_ACT_DATA = output;

    var blob = new Blob([JSON.stringify(output, null, 2)], {type: 'application/json'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'income_tax_act_1961.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    var withText = unique.filter(function(s) { return s.full_text.length > 50; }).length;
    console.log('\n===================================================');
    console.log('[IT-Scraper] DONE! ' + unique.length + ' sections');
    console.log('[IT-Scraper] With full text (>50 chars): ' + withText);
    console.log('[IT-Scraper] Description only: ' + (unique.length - withText));
    console.log('[IT-Scraper] JSON downloaded as income_tax_act_1961.json');
    console.log('[IT-Scraper] Save to: data/Law/IT_ACT/income_tax_act_1961.json');
    console.log('===================================================');
})();
