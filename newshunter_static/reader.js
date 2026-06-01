/* Leitor de artigos in-dash — abre modal ao clicar em qualquer .article-link */
(function () {
  'use strict';

  // AbortController do fetch em andamento.
  // Quando o usuário troca de artigo antes do anterior carregar, o fetch anterior
  // é cancelado para evitar que o conteúdo antigo sobreponha o novo.
  var _currentController = null;

  function getModal()   { return document.getElementById('reader-modal'); }
  function getContent() { return document.getElementById('reader-content'); }
  function getExtLink() { return document.getElementById('reader-ext-link'); }

  window.openReader = function (ev, el) {
    if (ev) ev.preventDefault();
    var url = el.getAttribute('data-reader-url') || el.href || '';
    if (!url) return;

    // Cancela fetch anterior — evita race condition ao navegar rapidamente
    if (_currentController) {
      _currentController.abort();
      _currentController = null;
    }

    var modal   = getModal();
    var content = getContent();
    var extLink = getExtLink();
    if (!modal || !content) {
      window.open(url, '_blank', 'noopener');
      return;
    }

    if (extLink) extLink.href = url;

    // Mostra loading IMEDIATAMENTE — limpa qualquer conteúdo anterior
    content.innerHTML = '<p class="reader-loading">⏳ Carregando artigo…</p>';
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';

    var controller = new AbortController();
    _currentController = controller;

    fetch('/api/article?url=' + encodeURIComponent(url), { signal: controller.signal })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        // Ignora resposta se já foi cancelada (usuário abriu outro artigo)
        if (controller.signal.aborted) return;
        _currentController = null;
        content.innerHTML = html;
      })
      .catch(function (e) {
        if (e.name === 'AbortError') return; // cancelado pelo usuário — normal
        if (controller.signal.aborted) return;
        _currentController = null;
        content.innerHTML =
          '<p class="reader-error">Erro ao carregar: ' + e.message + '</p>';
      });
  };

  window.closeReader = function () {
    // Cancela fetch pendente ao fechar o modal
    if (_currentController) {
      _currentController.abort();
      _currentController = null;
    }
    var modal = getModal();
    if (modal) modal.classList.remove('open');
    document.body.style.overflow = '';
    var content = getContent();
    if (content) content.innerHTML = '';
  };

  // Fecha ao pressionar Escape
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') window.closeReader();
  });

  // Re-registra links após HTMX trocas de fragmento
  document.body.addEventListener('htmx:afterSwap', function () {
    // Links já têm onclick inline — nada a fazer, o atributo persiste.
  });
})();
