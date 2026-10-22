import axios from 'axios';

// Starts the PDF design report for a site, waits for it, and saves the file.
// The download goes through axios (not a plain link) so the API key header is sent.
export async function createPdfReport({ website, company, apiBase = '' }) {
  const { data } = await axios.post(`${apiBase}/api/report`, { website, company });
  const started = Date.now();
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const { data: status } = await axios.get(`${apiBase}/api/report/${data.job_id}`);
    if (status.status === 'done') {
      const pdf = await axios.get(`${apiBase}/api/report/${data.job_id}/pdf`, { responseType: 'blob' });
      const url = URL.createObjectURL(pdf.data);
      const link = document.createElement('a');
      link.href = url;
      link.download = status.filename || 'report.pdf';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      return status;
    }
    if (status.status === 'failed') throw new Error(status.error || 'The report could not be created.');
    if (Date.now() - started > 10 * 60 * 1000) throw new Error('The report is taking too long. Try again.');
  }
}

export function reportErrorMessage(err) {
  return err?.response?.data?.detail || err?.message || 'The report could not be created.';
}
