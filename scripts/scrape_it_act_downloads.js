// ==========================================================================
// Nyaya Ledger — Income-tax Act 1961 Download URL Collector
// ==========================================================================
//
// This script clicks each "Download PDF" button on the sections list and
// captures the resulting download URL. It outputs a JSON with all URLs.
//
// USAGE:
//   1. Open https://www.incometaxindia.gov.in/income-tax-act in Chrome
//   2. Select "Income-tax Act, 1961" and latest year (2026)
//   3. Wait for sections to load
//   4. F12 → Console → Paste this script → Enter
//   5. It will paginate through all 94 pages, clicking each download button
//   6. A JSON file auto-downloads at the end
//
// Save the file to: data/Law/IT_ACT/section_download_urls.json
// ==========================================================================

(async function ITActDownloadCollector() {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const RESULTS = [];

    // Intercept window.open to capture download URLs
    const originalWindowOpen = window.open;
    window.open = function(url, ...args) {
        if (url) {
            RESULTS.push({ url: url, captured_via: 'window.open' });
        }
        return null;
    };

    // Also intercept link clicks that might trigger downloads
    document.addEventListener('click', function(e) {
        const link = e.target.closest('a[href]');
        if (link && link.href) {
            const href = link.href;
            if (href.includes('Section') || href.includes('.pdf') || href.includes('download')) {
                RESULTS.push({ url: href, captured_via: 'link_click' });
            }
        }
    }, true);

    console.log('[IT-Downloads] Download interceptor installed');

    function scrapeVisibleSections() {
        const items = document.querySelectorAll('.sections-list .sections-item');
        const results = [];
        items.forEach(item => {
            const nameEl = item.querySelector('.section-name');
            const descEl = item.querySelector('.section-desc');
            const downloadBtn = item.querySelector('button.download');
            const name = nameEl?.textContent?.trim() || '';
            const desc = descEl?.getAttribute('title') || descEl?.textContent?.trim() || '';
            const match = name.match(/Section\s*[-–]\s*(.+)/i);
            results.push({
                section_number: match ? match[1].trim() : name,
                section_label: name,
                description: desc,
                downloadBtn: downloadBtn,
            });
        });
        return results;
    }

    // Go to page 1
    const pageInput = document.querySelector(
        '.pagination-wrapper:not(.mobile-view) #pagination-input-box'
    );
    const pagesEl = document.querySelector(
        '.pagination-wrapper:not(.mobile-view) .total-pages'
    );
    const totalPages = parseInt(pagesEl?.textContent?.replace(/[^\d]/g, '') || '94');

    if (pageInput) {
        pageInput.value = '1';
        pageInput.dispatchEvent(new Event('input', { bubbles: true }));
        pageInput.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true
        }));
        await sleep(2000);
    }

    console.log(`[IT-Downloads] ${totalPages} pages to process`);

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
            continue;
        }

        // Click each download button to capture the URL
        for (const item of items) {
            const beforeCount = RESULTS.length;
            if (item.downloadBtn) {
                item.downloadBtn.click();
                await sleep(500);
            }
            item.download_url = RESULTS.length > beforeCount
                ? RESULTS[RESULTS.length - 1].url
                : null;
        }

        console.log(
            `[IT-Downloads] Page ${p}/${totalPages}: ${items.length} sections, ` +
            `${RESULTS.length} total URLs captured`
        );
    }

    // Restore window.open
    window.open = originalWindowOpen;

    // Build final output
    const sections = [];
    const items = document.querySelectorAll('.sections-list .sections-item');

    // Re-collect all section data with URLs
    for (let i = 0; i < RESULTS.length; i++) {
        const url = RESULTS[i].url;
        // Extract section number from URL filename
        const match = url.match(/Section[-_](\d+[A-Za-z]*)/i);
        sections.push({
            section_number: match ? match[1] : String(i + 1),
            download_url: url,
            filename: url.split('/').pop()?.split('?')[0] || '',
        });
    }

    const output = {
        source: 'https://www.incometaxindia.gov.in/income-tax-act',
        act: 'Income-tax Act, 1961',
        total_urls: sections.length,
        scraped_at: new Date().toISOString(),
        sections: sections,
    };

    window.__IT_ACT_DOWNLOADS = output;

    const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'section_download_urls.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    console.log(`\n===================================================`);
    console.log(`[IT-Downloads] DONE! ${sections.length} download URLs captured.`);
    console.log(`[IT-Downloads] JSON downloaded as section_download_urls.json`);
    console.log(`[IT-Downloads] Save to: data/Law/IT_ACT/section_download_urls.json`);
    if (sections.length > 0) {
        console.log(`[IT-Downloads] Sample URL: ${sections[0].download_url}`);
    }
    console.log(`===================================================`);
})();
