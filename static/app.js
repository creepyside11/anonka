const $ = (id) => document.getElementById(id);
const state = { profile: null, settings: null, repos: [], branches: [] };

function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function formatDate(value) {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'medium' });
}

function switchView(view) {
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('active', el.id === `${view}-view`));
  document.querySelectorAll('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.view === view));
  $('page-title').textContent = view === 'profile' ? 'Профиль' : 'Настройки проекта';
  history.replaceState(null, '', view === 'profile' ? '/profile' : '/');
}

document.querySelectorAll('[data-view]').forEach((button) => button.addEventListener('click', () => switchView(button.dataset.view)));

function renderProfile() {
  const profile = state.profile;
  const connected = profile?.connected;
  $('connect-github').classList.toggle('hidden', connected);
  $('disconnect-github').classList.toggle('hidden', !connected);
  $('git-state').textContent = connected ? 'GitHub подключён' : 'Не подключён';
  $('git-state').classList.toggle('muted', !connected);
  $('oauth-badge').textContent = connected ? 'Подключено' : 'Отключено';
  $('oauth-badge').classList.toggle('muted', !connected);
  $('repo-select').disabled = !connected;
  $('auto-toggle').disabled = !connected;
  $('auto-note').textContent = connected ? 'Проверка ветки каждые 30 секунд' : 'Доступно после подключения GitHub';

  if (!profile?.oauth_configured) {
    $('oauth-warning').textContent = 'На сервере не заданы GITHUB_CLIENT_ID и GITHUB_CLIENT_SECRET.';
    $('oauth-warning').classList.remove('hidden');
    $('connect-github').classList.add('hidden');
  } else {
    $('oauth-warning').classList.add('hidden');
  }

  if (connected) {
    const account = profile.account;
    $('profile-name').textContent = `@${account.login}`;
    $('profile-heading').textContent = `@${account.login}`;
    $('profile-description').textContent = 'GitHub подключён. Доступны публичные и приватные репозитории аккаунта.';
    const image = `<img src="${account.avatar}" alt="${account.login}">`;
    $('profile-avatar').innerHTML = image;
    document.querySelector('.profile-chip .avatar-fallback').outerHTML = `<img class="avatar-fallback" src="${account.avatar}" alt="${account.login}">`;
  } else {
    $('profile-name').textContent = 'GitHub не подключён';
    $('profile-heading').textContent = 'GitHub не подключён';
    $('profile-description').textContent = 'Подключи аккаунт GitHub, чтобы видеть приватные репозитории и доступные ветки.';
  }
}

async function loadProfile() {
  state.profile = await api('/api/profile');
  renderProfile();
  if (state.profile.connected) await Promise.all([loadRepos(), loadSettings()]);
}

async function loadRepos() {
  const select = $('repo-select');
  select.innerHTML = '<option>Загрузка…</option>';
  try {
    state.repos = await api('/api/github/repos');
    select.innerHTML = '<option value="">Выберите репозиторий</option>' + state.repos.map((repo) =>
      `<option value="${repo.full_name}">${repo.private ? '🔒' : '◌'} ${repo.full_name}</option>`
    ).join('');
    if (state.settings?.repo) {
      select.value = state.settings.repo;
      await loadBranches(state.settings.repo, state.settings.branch);
    }
  } catch (error) {
    select.innerHTML = '<option>Ошибка загрузки</option>';
    toast(error.message);
  }
}

async function loadBranches(repo, selected = '') {
  const select = $('branch-select');
  if (!repo) {
    select.disabled = true;
    select.innerHTML = '<option>—</option>';
    updateButtons();
    return;
  }
  select.disabled = true;
  select.innerHTML = '<option>Загрузка…</option>';
  try {
    state.branches = await api(`/api/github/branches?repo=${encodeURIComponent(repo)}`);
    select.innerHTML = '<option value="">Выберите ветку</option>' + state.branches.map((branch) =>
      `<option value="${branch.name}">${branch.name}</option>`
    ).join('');
    select.disabled = false;
    if (selected) select.value = selected;
  } catch (error) {
    select.innerHTML = '<option>Ошибка загрузки</option>';
    toast(error.message);
  }
  updateButtons();
}

async function loadSettings() {
  try {
    state.settings = await api('/api/project/settings');
    $('auto-toggle').checked = !!state.settings.auto_update;
    $('last-checked').textContent = formatDate(state.settings.last_checked);
    $('last-synced').textContent = formatDate(state.settings.last_synced);
    $('sync-error').classList.toggle('hidden', !state.settings.last_error);
    $('sync-error').textContent = state.settings.last_error || '';
    if (state.repos.length && state.settings.repo) {
      $('repo-select').value = state.settings.repo;
      await loadBranches(state.settings.repo, state.settings.branch);
    }
    updateButtons();
  } catch (error) {
    toast(error.message);
  }
}

function updateButtons() {
  const ready = !!(state.profile?.connected && $('repo-select').value && $('branch-select').value);
  $('save-settings').disabled = !ready;
  $('sync-now').disabled = !ready;
}

$('repo-select').addEventListener('change', async (event) => {
  const repo = event.target.value;
  const meta = state.repos.find((item) => item.full_name === repo);
  await loadBranches(repo, meta?.default_branch || '');
});
$('branch-select').addEventListener('change', updateButtons);

$('save-settings').addEventListener('click', async () => {
  $('save-status').textContent = 'Сохраняю…';
  try {
    const result = await api('/api/project/settings', {
      method: 'POST',
      body: JSON.stringify({
        repo: $('repo-select').value,
        branch: $('branch-select').value,
        auto_update: $('auto-toggle').checked,
      }),
    });
    state.settings = result.settings;
    $('save-status').textContent = $('auto-toggle').checked ? 'Автообновление включено.' : 'Настройки сохранены.';
    toast('Настройки сохранены');
  } catch (error) {
    $('save-status').textContent = error.message;
  }
});

$('sync-now').addEventListener('click', async () => {
  const btn = $('sync-now');
  btn.disabled = true;
  btn.textContent = 'Обновляю…';
  try {
    await api('/api/project/settings', {
      method: 'POST',
      body: JSON.stringify({
        repo: $('repo-select').value,
        branch: $('branch-select').value,
        auto_update: $('auto-toggle').checked,
      }),
    });
    await api('/api/project/sync-now', { method: 'POST' });
    await loadSettings();
    toast('Проект обновлён');
  } catch (error) {
    toast(error.message);
  } finally {
    btn.textContent = 'Обновить сейчас';
    updateButtons();
  }
});

$('disconnect-github').addEventListener('click', async () => {
  await api('/api/github/disconnect', { method: 'POST' });
  state.profile = null; state.settings = null; state.repos = []; state.branches = [];
  $('repo-select').innerHTML = '<option>Сначала подключите GitHub</option>';
  $('branch-select').innerHTML = '<option>—</option>';
  $('auto-toggle').checked = false;
  await loadProfile();
  toast('GitHub отключён');
});

setInterval(async () => {
  if (state.profile?.connected) await loadSettings();
}, 30000);

if (location.pathname === '/profile') switchView('profile');
loadProfile().catch((error) => toast(error.message));
