const exportStatus = document.getElementById('export-status');
const retryExport = document.getElementById('export-retry');
const quickExport = document.getElementById('export-quick');
const fullExport = document.getElementById('export-full');
let lastReport = true;

async function exportExcel(quick) {
  lastReport = quick;
  quickExport.disabled = fullExport.disabled = true;
  retryExport.hidden = true;
  exportStatus.textContent = quick
    ? 'Preparing the quick report. You can keep using the dashboard.'
    : 'Preparing the full audit workbook. Large histories can take several minutes.';
  try {
    const response = await fetch(quick ? '/api/export/quick.xlsx' : '/api/export.xlsx', {cache: 'no-store'});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || 'Excel export failed. Please try again.');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = quick ? 'BTC-Prophet-Quick-Report.xlsx' : 'BTC-Prophet-Paper-Report.xlsx';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    exportStatus.textContent = 'Download started. Check your Downloads folder.';
  } catch (error) {
    exportStatus.textContent = error.message;
    retryExport.hidden = false;
  } finally {
    quickExport.disabled = fullExport.disabled = false;
  }
}

retryExport.addEventListener('click', () => exportExcel(lastReport));
quickExport.addEventListener('click', () => exportExcel(true));
fullExport.addEventListener('click', () => exportExcel(false));
exportExcel(true);
