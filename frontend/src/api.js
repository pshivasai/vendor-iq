const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const contentType = response.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof body === 'object' ? (body.detail || JSON.stringify(body)) : body;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return body;
}

export const api = {
  health: () => request('/api/health'),
  stats: () => request('/api/dashboard/stats'),
  list: () => request('/api/submissions'),
  get: (id) => request(`/api/submissions/${id}`),
  create: (payload) => request('/api/submissions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  upload: (id, file, documentType) => {
    const form = new FormData(); form.append('file', file); form.append('document_type', documentType);
    return request(`/api/submissions/${id}/documents`, { method: 'POST', body: form });
  },
  evaluate: (id) => request(`/api/submissions/${id}/evaluate`, { method: 'POST' }),
  audit: (id) => request(`/api/submissions/${id}/audit`),
  sample: (variant = 1) => request(`/api/demo/sample?variant=${variant}`),
  // Admin dashboard
  adminStats: () => request('/api/admin/stats'),
  adminDecision: (id, decision) => request(`/api/admin/submissions/${id}/decision`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  }),
};
