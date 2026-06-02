// ==========================================================================
// Nyaya Ledger — Income-tax Act 1961 API Interceptor
// ==========================================================================
//
// This script intercepts the Liferay Search API that the React component
// uses internally, so we get structured section data including download URLs.
//
// USAGE:
//   1. Open https://www.incometaxindia.gov.in/income-tax-act in Chrome
//   2. Select "Income-tax Act, 1961" and latest year (2026)
//   3. Wait for sections list to appear
//   4. F12 → Console → Paste this script → Enter
//   5. The script will:
//      a) Monkey-patch fetch() to capture the search API response
//      b) Click through all 94 pages to trigger API calls
//      c) Collect all section data with PDF download URLs
//      d) Auto-download a JSON file at the end
//
//   Save the downloaded file as:
//     data/Law/base_laws/income_tax_act_1961_api.json
//
//   Then run: python3 scripts/ingest_it_act.py
// ==========================================================================

(async function ITActAPIScraper() {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const ALL_SECTIONS = [];
    let capturedResponses = [];

    // -------------------------------------------------------
    // STEP 1: Monkey-patch fetch to intercept API responses
    // -------------------------------------------------------
    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        const response = await originalFetch.apply(this, args);
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';

        // Intercept search/API responses that contain section data
        if (url.includes('/o/') || url.includes('search') || url.includes('documents')) {
            try {
                const clone = response.clone();
                const text = await clone.text();
                if (text && text.startsWith('{')) {
                    const data = JSON.parse(text);
                    // Check if this looks like section data
                    if (data.items || data.results || Array.isArray(data)) {
                        capturedResponses.push({
                            url: url.substring(0, 300),
                            data: data,
                            timestamp: Date.now()
                        });
                    }
                }
            } catch(e) { /* not JSON, ignore */ }
        }
        return response;
    };

    console.log('[IT-Scraper] Fetch interceptor installed');

    // -------------------------------------------------------
    // STEP 2: Also try calling the API directly
    // -------------------------------------------------------
    // The React app knows the ERC for the search blueprint.
    // Let's try common Liferay headless custom object endpoints.

    const ACT_ID = document.querySelector('input[name="choose-act"]')?.value;
    const YEAR_ID = document.querySelector('input[name="choose-year"]')?.value;
    console.log(`[IT-Scraper] Act ID: ${ACT_ID}, Year ID: ${YEAR_ID}`);

    async function tryAPIEndpoints() {
        const endpoints = [
            // Liferay custom object endpoints
            `/o/c/itactsections/?pageSize=-1&filter=actId%20eq%20%27${ACT_ID}%27%20and%20yearId%20eq%20%27${YEAR_ID}%27&sort=sectionNumber:asc`,
            `/o/c/actsections/?pageSize=-1&filter=actId%20eq%20%27${ACT_ID}%27&sort=sectionNumber:asc`,
            `/o/c/documents/?pageSize=-1&filter=actId%20eq%20%27${ACT_ID}%27`,
            // Headless delivery
            `/o/headless-delivery/v1.0/sites/20117/documents?pageSize=100`,
        ];

        for (const ep of endpoints) {
            try {
                console.log(`[IT-Scraper] Trying: ${ep.substring(0, 80)}...`);
                const resp = await originalFetch(ep, {
                    headers: { 'Accept': 'application/json' }
                });
                console.log(`  → Status: ${resp.status}`);
                if (resp.ok) {
                    const data = await resp.json();
                    const count = data.items?.length || data.totalCount || 0;
                    console.log(`  → Items: ${count}`);
                    if (count > 0) {
                        console.log(`  → Sample keys: ${Object.keys(data.items[0]).join(', ')}`);
                        console.log(`  → Sample:`, JSON.stringify(data.items[0]).substring(0, 500));
                        return { endpoint: ep, data };
                    }
                }
            } catch(e) {
                console.log(`  → Error: ${e.message}`);
            }
        }
        return null;
    }

    const apiResult = await tryAPIEndpoints();

    if (apiResult && apiResult.data.items?.length > 50) {
        // Found the API! Use it directly
        console.log(`[IT-Scraper] Found working API endpoint with ${apiResult.data.items.length} items`);
        ALL_SECTIONS.push(...apiResult.data.items);

        // Check if paginated
        if (apiResult.data.lastPage > 1) {
            for (let p = 2; p <= apiResult.data.lastPage; p++) {
                const resp = await originalFetch(
                    `${apiResult.endpoint}&page=${p}`,
                    { headers: { 'Accept': 'application/json' } }
                );
                if (resp.ok) {
                    const data = await resp.json();
                    ALL_SECTIONS.push(...(data.items || []));
                    console.log(`[IT-Scraper] Page ${p}: +${data.items?.length || 0}`);
                }
                await sleep(300);
            }
        }
    } else {
        // -------------------------------------------------------
        // STEP 3: Fall back to DOM scraping + click interception
        // -------------------------------------------------------
        console.log('[IT-Scraper] API not directly accessible. Using DOM pagination...');

        function scrapeVisibleSections() {
            const items = document.querySelectorAll('.sections-list .sections-item');
            const results = [];
            items.forEach(item => {
                const nameEl = item.querySelector('.section-name');
                const descEl = item.querySelector('.section-desc');
                const name = nameEl?.textContent?.trim() || '';
                const desc = descEl?.getAttribute('title') || descEl?.textContent?.trim() || '';
                const match = name.match(/Section\s*[-–]\s*(.+)/i);
                results.push({
                    section_number: match ? match[1].trim() : name,
                    section_label: name,
                    description: desc,
                });
            });
            return results;
        }

        // Get total pages
        const pagesEl = document.querySelector(
            '.pagination-wrapper:not(.mobile-view) .total-pages'
        );
        const totalPages = parseInt(pagesEl?.textContent?.replace(/[^\d]/g, '') || '94');
        const pageInput = document.querySelector(
            '.pagination-wrapper:not(.mobile-view) #pagination-input-box'
        );

        console.log(`[IT-Scraper] ${totalPages} pages to scrape`);

        // Go to page 1
        if (pageInput) {
            pageInput.value = '1';
            pageInput.dispatchEvent(new Event('input', { bubbles: true }));
            pageInput.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true
            }));
            await sleep(2000);
        }

        for (let p = 1; p <= totalPages; p++) {
            if (p > 1 && pageInput) {
                pageInput.focus();
                pageInput.value = '';
                pageInput.dispatchEvent(new Event('input', { bubbles: true }));
                await sleep(100);
                pageInput.value = String(p);
                pageInput.dispatchEvent(new Event('input', { bubbles: true }));
                await sleep(100);
                pageInput.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true
                }));
                await sleep(2500);
            }

            const items = scrapeVisibleSections();
            if (items.length === 0) {
                await sleep(2000);
                const retry = scrapeVisibleSections();
                if (retry.length === 0) {
                    console.warn(`[IT-Scraper] Page ${p}: empty, skipping`);
                    continue;
                }
                ALL_SECTIONS.push(...retry);
            } else {
                ALL_SECTIONS.push(...items);
            }
            console.log(`[IT-Scraper] Page ${p}/${totalPages}: +${items.length} (total: ${ALL_SECTIONS.length})`);
        }
    }

    // -------------------------------------------------------
    // STEP 4: Try to get full text + PDF URLs for each section
    // -------------------------------------------------------
    console.log(`[IT-Scraper] Collected ${ALL_SECTIONS.length} section entries`);
    console.log('[IT-Scraper] Now fetching section detail pages for full text...');

    let textOk = 0, textFail = 0;

    for (let i = 0; i < ALL_SECTIONS.length; i++) {
        const sec = ALL_SECTIONS[i];
        const num = (sec.section_number || sec.title || String(i + 1))
            .toLowerCase().replace(/\s+/g, '-');

        const urls = [
            `/income-tax-act/section-${num}`,
            `/income-tax-act/section%20-%20${sec.section_number || (i + 1)}`,
        ];

        for (const u of urls) {
            try {
                const resp = await originalFetch(u);
                if (!resp.ok) continue;
                const html = await resp.text();
                if (html.length < 500 || html.includes('Access Denied')) continue;

                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');

                // Try multiple selectors for content
                const selectors = [
                    '.section-detail-content',
                    '.section-content',
                    '.act-section-content',
                    '.etds-section-detail',
                    '[class*="sectionContent"]',
                    '[class*="detail-content"]',
                    'etds-act-details-custom-element',
                    'main .content',
                    '#main-content',
                ];

                let found = false;
                for (const sel of selectors) {
                    const el = doc.querySelector(sel);
                    if (el && el.textContent.trim().length > 100) {
                        sec.full_text = el.textContent.trim();
                        // Check for PDF download link
                        const pdfLink = el.querySelector('a[href*="download"], a[href*=".pdf"]');
                        if (pdfLink) sec.pdf_url = pdfLink.getAttribute('href');
                        found = true;
                        break;
                    }
                }

                // Fallback: extract meaningful text from body
                if (!found) {
                    const body = doc.querySelector('body');
                    if (body) {
                        const clone = body.cloneNode(true);
                        clone.querySelectorAll('script, style, noscript, nav, header, footer').forEach(e => e.remove());
                        const text = clone.textContent.trim();
                        if (text.length > 200) {
                            sec.full_text = text;
                            found = true;
                        }
                    }
                }

                if (found) break;
            } catch(e) { /* next URL */ }
        }

        if (sec.full_text) textOk++; else textFail++;

        if ((i + 1) % 50 === 0 || i === ALL_SECTIONS.length - 1) {
            console.log(`[IT-Scraper] Text: ${i+1}/${ALL_SECTIONS.length} (ok: ${textOk}, fail: ${textFail})`);
        }
        await sleep(150);
    }

    // Restore original fetch
    window.fetch = originalFetch;

    // -------------------------------------------------------
    // STEP 5: Save results
    // -------------------------------------------------------
    // Also include any captured API responses for analysis
    const output = {
        source: 'https://www.incometaxindia.gov.in/income-tax-act',
        act: 'Income-tax Act, 1961',
        act_id: ACT_ID,
        year_id: YEAR_ID,
        total_sections: ALL_SECTIONS.length,
        sections_with_text: textOk,
        sections_without_text: textFail,
        scraped_at: new Date().toISOString(),
        captured_api_responses: capturedResponses.length,
        sections: ALL_SECTIONS,
    };

    if (capturedResponses.length > 0) {
        output.api_debug = capturedResponses.slice(0, 5).map(r => ({
            url: r.url,
            keys: Object.keys(r.data),
            sample: JSON.stringify(r.data).substring(0, 2000)
        }));
    }

    window.__IT_ACT_FINAL = output;

    const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'income_tax_act_1961.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    console.log('\n===================================================');
    console.log(`[IT-Scraper] COMPLETE!`);
    console.log(`[IT-Scraper] Sections: ${ALL_SECTIONS.length}`);
    console.log(`[IT-Scraper] With text: ${textOk}`);
    console.log(`[IT-Scraper] Without text: ${textFail}`);
    console.log(`[IT-Scraper] Captured API responses: ${capturedResponses.length}`);
    if (capturedResponses.length > 0) {
        console.log(`[IT-Scraper] API response URLs:`);
        capturedResponses.forEach(r => console.log(`  ${r.url}`));
    }
    console.log(`[IT-Scraper] JSON downloaded as income_tax_act_1961.json`);
    console.log(`[IT-Scraper] Save to: data/Law/base_laws/income_tax_act_1961.json`);
    console.log('===================================================');
})();
