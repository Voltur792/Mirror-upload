(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const activeStates = new Set(['preparing', 'downloading', 'verifying', 'installing']);
  const stageLabels = {preparing: 'Подготовка', downloading: 'Загрузка', verifying: 'Проверка архива', installing: 'Установка', done: 'Готово', cancelled: 'Отменено', error: 'Не удалось установить'};
  const groupLabels = {voices: 'Голоса', voice: 'Голоса', tts: 'Голоса', recognition: 'Распознавание', stt: 'Распознавание', search: 'Поиск', embeddings: 'Поиск'};
  const preview = location.hostname !== 'astra-plugin.localhost' || !window.astra || typeof window.astra.callBackend !== 'function';
  let dashboard = null;
  let currentFilter = 'all';
  let selectedComponent = '';
  let busy = false;
  let polling = false;
  let pollTimer = null;
  let disposed = false;
  let optionsSignature = '';
  let catalogSignature = '';
  let mountedRefresh = false;

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined && content !== null) element.textContent = String(content);
    return element;
  }

  function bytes(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount) || amount < 0) return 'Размер неизвестен';
    if (amount < 1024) return `${Math.round(amount).toLocaleString('ru-RU')} Б`;
    const units = ['КиБ', 'МиБ', 'ГиБ', 'ТиБ'];
    let scaled = amount / 1024;
    let unit = 0;
    while (scaled >= 1024 && unit < units.length - 1) { scaled /= 1024; unit += 1; }
    return `${scaled.toLocaleString('ru-RU', {maximumFractionDigits: 1})} ${units[unit]}`;
  }

  function isInstalling() { return !!dashboard && activeStates.has(dashboard.job?.state); }

  function callBackend(method, params = {}, timeoutMs = 25000) {
    if (preview) return Promise.reject(new Error('Установка доступна только внутри Astra.'));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Astra не ответила вовремя. Обновите состояние перед повторной попыткой.')), timeoutMs);
      Promise.resolve().then(() => window.astra.callBackend(method, params)).then(
        (result) => { clearTimeout(timer); resolve(result); },
        () => { clearTimeout(timer); reject(new Error('Не удалось связаться с плагином. Проверьте, что он запущен в Astra.')); }
      );
    });
  }

  function feedback(message, error = false) {
    $('action-feedback').textContent = message;
    $('action-feedback').dataset.error = String(error);
    $('action-feedback').hidden = !message;
  }

  function setControls() {
    const active = isInstalling();
    const mirrorLoading = dashboard?.mirror_state === 'loading';
    $('refresh').disabled = preview || busy || active || mirrorLoading || !dashboard?.mirror_configured;
    $('cancel').disabled = preview || busy || !active;
    $('restore-environment').disabled = preview || busy || active || !dashboard?.quick_setup?.environment_saved;
    $('quick-setup').disabled = preview || busy || active || mirrorLoading || !dashboard?.quick_setup?.supported || !!dashboard?.quick_setup?.ready;
    $('component-choice').disabled = preview || busy || active || mirrorLoading || !(dashboard?.components || []).some((component) => !component.installed);
    $('local-path').disabled = preview || busy || active || mirrorLoading;
    $('import-local').disabled = preview || busy || active || mirrorLoading || !selectedComponent;
    document.querySelectorAll('[data-install]').forEach((button) => {
      const component = (dashboard?.components || []).find((entry) => String(entry.id) === button.dataset.install);
      button.disabled = preview || busy || active || !component || !!component.installed || !component.available;
    });
  }

  function normalizeGroup(group) {
    const value = String(group || '').toLowerCase();
    if (['voices', 'voice', 'tts', 'голоса'].includes(value)) return 'voices';
    if (['recognition', 'stt', 'asr', 'распознавание', 'распознавание речи'].includes(value)) return 'recognition';
    if (['search', 'embeddings', 'embedding', 'поиск'].includes(value)) return 'search';
    return value || 'other';
  }

  function renderCatalog() {
    const catalog = $('catalog');
    const components = Array.isArray(dashboard?.components) ? dashboard.components : [];
    const signature = JSON.stringify([currentFilter, components, dashboard?.mirror_configured, dashboard?.mirror_state, dashboard?.mirror_message]);
    if (signature === catalogSignature) { setControls(); return; }
    catalogSignature = signature;
    const focusedComponent = document.activeElement?.dataset?.install;
    catalog.replaceChildren();
    catalog.setAttribute('aria-busy', 'false');
    const filtered = components.filter((component) => currentFilter === 'all' || normalizeGroup(component.group) === currentFilter);
    $('catalog-count').textContent = `${filtered.length} из ${components.length}`;
    if (!filtered.length) {
      const empty = node('div', 'empty-state');
      const state = dashboard?.mirror_state;
      const categoryEmpty = currentFilter !== 'all' && components.length > 0;
      const title = categoryEmpty ? 'В этой категории пока нет компонентов' : state === 'error' ? 'Каталог зеркала недоступен' : state === 'loading' ? 'Проверяем папку на Диске' : state === 'ready' ? 'В каталоге нет компонентов' : dashboard?.mirror_configured ? 'Зеркало ещё не проверено' : 'Каталог пока пуст';
      empty.append(node('h3', '', title));
      const explanation = categoryEmpty ? 'Выберите другую категорию.' : state === 'error'
        ? `${dashboard?.mirror_message || 'Не удалось прочитать каталог.'} Проверьте подключение к интернету и повторите обновление каталога позже.`
        : state === 'loading' ? 'Получаем каталог компонентов. Дождитесь результата проверки.'
        : !dashboard?.mirror_configured ? 'Источник компонентов временно недоступен. Повторите обновление позже.'
        : 'Обновите каталог, чтобы увидеть модели в зеркале. Если список остаётся пустым, повторите обновление позже.';
      empty.append(node('p', '', explanation));
      catalog.append(empty);
      return;
    }
    const groups = new Map();
    for (const component of filtered) {
      const group = normalizeGroup(component.group);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(component);
    }
    for (const [group, entries] of groups) {
      const section = node('section', 'component-group');
      section.append(node('h3', 'group-title', groupLabels[group] || String(entries[0].group || 'Другие модели')));
      for (const component of entries) {
        const row = node('div', 'component-row');
        const copy = node('div', 'component-copy');
        copy.append(node('p', 'component-name', component.name));
        if (component.description) copy.append(node('p', 'component-description', component.description));
        const meta = node('div', 'component-meta');
        meta.append(node('span', '', bytes(component.size_bytes)));
        const unavailable = dashboard?.mirror_state === 'empty' ? 'Зеркало не проверено' : dashboard?.mirror_state === 'loading' ? 'Проверяем зеркало' : dashboard?.mirror_state === 'error' ? 'Зеркало недоступно' : 'Недоступно сейчас';
        const availability = component.installed ? 'Файлы на месте' : component.available ? 'Есть в зеркале' : unavailable;
        meta.append(node('span', `availability${component.installed ? ' installed' : ''}`, availability));
        copy.append(meta);
        const notes = Array.isArray(component.notes) ? component.notes.filter(Boolean).map(String).join(' ') : String(component.notes || '');
        if (notes) copy.append(node('p', 'component-note', notes));
        if (component.activation_required && !/активац|activat/i.test(notes)) copy.append(node('p', 'component-note', 'Активация Vox остаётся в Astra. Данные аккаунта не переносятся через зеркало.'));
        const actionLabel = component.installed ? 'Файлы на месте' : component.available ? 'Установить' : 'Недоступно';
        const install = node('button', `button secondary row-action${component.installed ? ' installed' : ''}`, actionLabel);
        install.type = 'button';
        install.dataset.install = String(component.id);
        install.setAttribute('aria-label', `${actionLabel}: ${component.name}`);
        row.append(copy, install);
        section.append(row);
      }
      catalog.append(section);
    }
    setControls();
    if (focusedComponent) {
      [...catalog.querySelectorAll('[data-install]')].find((button) => button.dataset.install === focusedComponent && !button.disabled)?.focus();
    }
  }

  function renderJob() {
    const job = dashboard?.job || {state: 'idle'};
    const visible = job.state !== 'idle';
    $('job-panel').hidden = !visible;
    if (!visible) return;
    const active = activeStates.has(job.state);
    const numericPercent = Number(job.percent);
    const percent = Math.max(0, Math.min(100, Number.isFinite(numericPercent) ? numericPercent : 0));
    $('job-panel').dataset.state = job.state;
    const setup = job.job_type === 'quick_setup';
    const label = String(job.stage_label || stageLabels[job.state] || 'Установка');
    const step = setup && Number(job.steps_total) > 0 ? `Шаг ${Number(job.step_index) || 1} из ${Number(job.steps_total)} · ` : '';
    $('job-stage').textContent = step + label;
    $('job-title').textContent = setup ? 'Быстрая настройка Астры' : job.component_name || 'Установка компонента';
    $('job-percent').textContent = `${Math.round(percent)}%${setup ? ' шага' : ''}`;
    $('job-bytes').textContent = `${bytes(job.downloaded_bytes || 0)} / ${job.total_bytes ? bytes(job.total_bytes) : 'размер уточняется'}`;
    $('job-progress-fill').style.width = `${percent}%`;
    $('job-progress').setAttribute('aria-valuenow', String(Math.round(percent)));
    $('job-progress').setAttribute('aria-valuetext', `${step}${label}, ${Math.round(percent)} процентов${setup ? ' текущего шага' : ''}`);
    const message = String(job.message || '');
    if ($('job-message').textContent !== message) $('job-message').textContent = message;
    $('cancel').hidden = !active;
  }

  function closePicker(returnFocus = false) {
    $('component-options').hidden = true;
    $('component-choice').setAttribute('aria-expanded', 'false');
    if (returnFocus) $('component-choice').focus();
  }

  function renderPicker() {
    const eligible = (dashboard?.components || []).filter((component) => !component.installed);
    if (!eligible.some((component) => String(component.id) === selectedComponent)) selectedComponent = '';
    const signature = JSON.stringify(eligible.map((component) => [component.id, component.name]));
    if (signature !== optionsSignature) {
      optionsSignature = signature;
      $('component-options').replaceChildren();
      for (const component of eligible) {
        const option = node('button', 'choice-option', component.name);
        option.type = 'button';
        option.setAttribute('role', 'option');
        option.dataset.component = String(component.id);
        option.tabIndex = -1;
        $('component-options').append(option);
      }
    }
    const selection = eligible.find((component) => String(component.id) === selectedComponent);
    $('component-choice').textContent = selection?.name || 'Выберите компонент';
    $('component-options').querySelectorAll('[role="option"]').forEach((option) => option.setAttribute('aria-selected', String(option.dataset.component === selectedComponent)));
  }

  function render(data) {
    dashboard = data;
    const state = dashboard.mirror_state || 'empty';
    $('mirror-status').dataset.state = state;
    const message = String(dashboard.mirror_message || (state === 'empty' ? 'Папка ещё не подключена' : 'Каталог загружен'));
    if ($('mirror-message').textContent !== message) $('mirror-message').textContent = message;
    renderJob();
    renderCatalog();
    renderPicker();
    const setup = dashboard.quick_setup || {};
    const setupMessage = String(setup.message || (setup.supported ? 'Готово к настройке' : 'Проверяем возможность настройки…'));
    if ($('setup-status').textContent !== setupMessage) $('setup-status').textContent = setupMessage;
    $('setup-status').parentElement.dataset.error = String(!setup.supported && !!setup.message);
    $('setup-download').textContent = setup.ready ? 'Повторная установка не требуется' : Number(setup.total_download_bytes) > 0 ? `До ${bytes(setup.total_download_bytes)} · зависит от установленных компонентов` : 'Размер загрузки уточняется';
    $('plugin-count').textContent = Number(setup.registry_plugins) > 0 ? String(setup.registry_plugins) : '22';
    $('setup-ready').hidden = !setup.ready;
    $('launcher-path').value = String(setup.launcher_path || '');
    $('launcher-path').hidden = !setup.launcher_path;
    $('restart-message').textContent = setup.restart_required ? 'Закройте Astra и один раз используйте ярлык «Mirror-upload - Завершить настройку» на рабочем столе. Он удалится после запуска; дальше запускайте Astra обычным способом.' : 'Настройка завершена. Astra можно запускать обычным способом. Временные установочные архивы удалены; необходимые модели и библиотеки сохранены.';
    $('data-root').textContent = dashboard.data_root || 'Путь станет доступен после подключения к Astra.';
    const info = Array.isArray(dashboard.info) && dashboard.info.length ? dashboard.info : ['Зеркало поддерживает локальные модели. Плагины, драйверы и облачные голоса устанавливаются отдельно.'];
    $('scope-info').replaceChildren(...info.map((line) => node('li', '', line)));
    setControls();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!disposed && !preview) pollTimer = setTimeout(poll, isInstalling() || dashboard?.mirror_state === 'loading' ? 2000 : 10000);
  }

  async function poll() {
    if (disposed || polling || preview) return;
    polling = true;
    try {
      const data = await callBackend('get_dashboard', {}, 12000);
      if (!disposed) {
        if (!data || !Array.isArray(data.components)) throw new Error('Плагин вернул неполное состояние. Попробуйте обновить каталог.');
        render(data);
      }
    } catch (error) {
      if (!disposed) {
        feedback(error.message, true);
        $('catalog').setAttribute('aria-busy', 'false');
        if (!dashboard) $('catalog').replaceChildren(node('p', 'empty-message', 'Состояние недоступно. Плагин повторит подключение автоматически.'));
      }
    } finally { polling = false; schedulePoll(); }
  }

  async function action(method, params) {
    if (busy || preview) return;
    busy = true;
    clearTimeout(pollTimer);
    feedback('');
    setControls();
    try {
      const result = await callBackend(method, params);
      if (!disposed) feedback(String(result?.message || (result?.ok ? 'Запрос принят.' : 'Не удалось выполнить действие.')), !result?.ok);
    } catch (error) { if (!disposed) feedback(error.message, true); }
    finally {
      busy = false;
      setControls();
      if (!polling) await poll();
      else schedulePoll();
    }
  }

  $('refresh').addEventListener('click', () => action('refresh_mirror'));
  $('cancel').addEventListener('click', () => action('cancel_install'));
  $('quick-setup').addEventListener('click', () => action('start_quick_setup'));
  $('restore-environment').addEventListener('click', () => action('restore_user_environment'));
  $('launcher-path').addEventListener('click', () => $('launcher-path').select());
  function selectTab(button, focus = false) {
    document.querySelectorAll('[data-tab]').forEach((entry) => {
      const selected = entry === button;
      entry.setAttribute('aria-selected', String(selected));
      entry.tabIndex = selected ? 0 : -1;
      $('panel-' + entry.dataset.tab).hidden = !selected;
    });
    closePicker();
    if (focus) button.focus();
  }
  document.querySelectorAll('[data-tab]').forEach((button) => {
    button.addEventListener('click', () => selectTab(button));
    button.addEventListener('keydown', (event) => {
      const tabs = [...document.querySelectorAll('[data-tab]')];
      if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (tabs.indexOf(button) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[index], true);
    });
  });
  $('catalog').addEventListener('click', (event) => {
    const button = event.target.closest('[data-install]');
    if (button && !button.disabled) action('install_component', {component_id: button.dataset.install});
  });
  document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
    currentFilter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((entry) => {
      const selected = entry === button;
      entry.classList.toggle('active', selected);
      entry.setAttribute('aria-pressed', String(selected));
    });
    renderCatalog();
  }));
  function openPicker() {
    $('component-options').hidden = false;
    $('component-choice').setAttribute('aria-expanded', 'true');
    const selected = $('component-options').querySelector('[aria-selected="true"]');
    (selected || $('component-options').firstElementChild)?.focus();
  }
  $('component-choice').addEventListener('click', () => {
    if ($('component-options').hidden) openPicker();
    else closePicker();
  });
  $('component-choice').addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); openPicker(); }
  });
  $('component-options').addEventListener('click', (event) => {
    const option = event.target.closest('[data-component]');
    if (!option) return;
    selectedComponent = option.dataset.component;
    renderPicker();
    closePicker(true);
    setControls();
  });
  $('component-options').addEventListener('keydown', (event) => {
    const options = [...$('component-options').children];
    let index = options.indexOf(document.activeElement);
    if (event.key === 'Escape') { event.preventDefault(); closePicker(true); }
    else if (event.key === 'Tab') closePicker();
    else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = options.length - 1;
      else index = (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      options[index]?.focus();
    }
  });
  document.addEventListener('click', (event) => { if (!event.target.closest('.component-picker')) closePicker(); });
  $('local-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const path = $('local-path').value.trim();
    if (!selectedComponent) { feedback('Сначала выберите компонент для установки.', true); return; }
    if (path) action('import_local', {component_id: selectedComponent, path});
  });
  window.addEventListener('pagehide', () => { disposed = true; clearTimeout(pollTimer); }, {once: true});

  if (preview) {
    $('preview-banner').hidden = false;
    render({mirror_configured: true, mirror_state: 'ready', mirror_message: 'Общее зеркало · каталог доступен (пример)', data_root: 'C:\\Users\\Пользователь\\AppData\\Roaming\\astra\\astra\\data\\models', quick_setup: {supported: true, message: 'Зеркало доступно · можно начать настройку', registry_plugins: 22, total_download_bytes: 4350000000, ready: false}, components: [
      {id: 'supertonic', name: 'Supertonic', group: 'voices', description: 'Локальное озвучивание текста, без соединения с облаком.', size_bytes: 267386880, installed: false, available: true},
      {id: 'whisper-small', name: 'Whisper · small', group: 'recognition', description: 'Модель распознавания речи для локальной обработки.', size_bytes: 483183820, installed: false, available: true},
      {id: 'embedding', name: 'Модель поиска', group: 'search', description: 'Локальная модель для семантического поиска.', size_bytes: 134217728, installed: true, available: false}
    ], job: {state: 'idle'}, info: ['Показаны примеры. Реальный список зависит от локальных файлов Astra и содержимого вашего зеркала.', 'Поддерживаются локальные модели. Плагины, драйверы и облачные голоса устанавливаются отдельно.']});
  } else {
    setControls();
    poll().then(() => {
      if (!disposed && !mountedRefresh && dashboard?.mirror_configured && !isInstalling() && dashboard.mirror_state !== 'loading') {
        mountedRefresh = true;
        action('refresh_mirror');
      }
    });
  }
})();
