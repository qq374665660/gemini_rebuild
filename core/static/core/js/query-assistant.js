(() => {
    const widget = document.querySelector('[data-ai-widget]');
    if (!widget || widget.dataset.ready === 'true') return;
    widget.dataset.ready = 'true';

    const launcher = widget.querySelector('[data-ai-launcher]');
    const panel = widget.querySelector('[data-ai-panel]');
    const closeButton = widget.querySelector('[data-ai-close]');
    const messages = widget.querySelector('[data-ai-messages]');
    const form = widget.querySelector('[data-ai-form]');
    const input = widget.querySelector('[data-ai-input]');
    const sendButton = widget.querySelector('[data-ai-send]');
    const modelSelect = widget.querySelector('[data-ai-model-select]');
    const modelStatus = widget.querySelector('[data-ai-model-status]');
    const csrfToken = form.querySelector('[name="csrfmiddlewaretoken"]')?.value || '';
    let requesting = false;
    const conversation = [];

    const setOpen = (open) => {
        widget.classList.toggle('is-open', open);
        launcher.setAttribute('aria-expanded', String(open));
        launcher.setAttribute('aria-label', open ? '关闭课题智能助手' : '打开课题智能助手');
        panel.setAttribute('aria-hidden', String(!open));
        if (open) window.setTimeout(() => input.focus(), 180);
    };

    const scrollToLatest = () => {
        window.requestAnimationFrame(() => {
            messages.scrollTop = messages.scrollHeight;
        });
    };

    const resizeInput = () => {
        input.style.height = 'auto';
        input.style.height = `${Math.min(input.scrollHeight, 112)}px`;
        sendButton.disabled = requesting || !input.value.trim();
    };

    const addUserMessage = (question) => {
        const message = document.createElement('div');
        message.className = 'ai-message ai-message-user';
        const bubble = document.createElement('div');
        bubble.className = 'ai-message-bubble';
        bubble.textContent = question;
        message.appendChild(bubble);
        messages.appendChild(message);
    };

    const addLoadingMessage = () => {
        const loading = document.createElement('div');
        loading.className = 'ai-message ai-message-assistant ai-message-loading';
        loading.dataset.aiLoading = 'true';
        loading.innerHTML = '<div class="ai-message-mini-avatar"><i class="fas fa-robot"></i></div>' +
            '<div class="ai-message-bubble"><span></span><span></span><span></span></div>';
        messages.appendChild(loading);
    };

    const addNetworkError = () => {
        const message = document.createElement('div');
        message.className = 'ai-message ai-message-assistant';
        message.innerHTML = '<div class="ai-message-mini-avatar"><i class="fas fa-robot"></i></div>' +
            '<div class="ai-message-content"><div class="ai-message-bubble ai-result-error">' +
            '<strong>查询没有成功</strong><span>服务暂时不可用，请稍后再试。</span></div></div>';
        messages.appendChild(message);
    };

    const ask = async (rawQuestion) => {
        const question = rawQuestion.trim();
        if (!question || requesting) return;

        requesting = true;
        input.value = '';
        resizeInput();
        addUserMessage(question);
        addLoadingMessage();
        scrollToLatest();

        try {
            const url = new URL(widget.dataset.queryUrl, window.location.origin);
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': csrfToken,
                },
                credentials: 'same-origin',
                body: JSON.stringify({
                    question,
                    history: conversation.slice(-8),
                    service_name: modelSelect?.value || '',
                    funding_category: widget.dataset.fundingCategory || '',
                }),
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            widget.querySelector('[data-ai-loading]')?.remove();
            messages.insertAdjacentHTML('beforeend', payload.html);
            conversation.push(
                {role: 'user', content: question},
                {role: 'assistant', content: payload.history_content || '已完成查询。'},
            );
            if (conversation.length > 12) conversation.splice(0, conversation.length - 12);
        } catch (error) {
            widget.querySelector('[data-ai-loading]')?.remove();
            addNetworkError();
        } finally {
            requesting = false;
            resizeInput();
            input.focus();
            scrollToLatest();
        }
    };

    launcher.addEventListener('click', () => setOpen(!widget.classList.contains('is-open')));
    closeButton.addEventListener('click', () => setOpen(false));
    form.addEventListener('submit', (event) => {
        event.preventDefault();
        ask(input.value);
    });
    input.addEventListener('input', resizeInput);
    modelSelect?.addEventListener('change', () => {
        const selected = modelSelect.options[modelSelect.selectedIndex];
        if (modelStatus) modelStatus.textContent = selected?.dataset.modelLabel || selected?.textContent || '本地规则模式';
        conversation.splice(0, conversation.length);
    });
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            ask(input.value);
        }
    });
    widget.addEventListener('click', (event) => {
        const suggestion = event.target.closest('[data-ai-question]');
        if (suggestion) ask(suggestion.dataset.aiQuestion || suggestion.textContent);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && widget.classList.contains('is-open')) setOpen(false);
    });
    resizeInput();
})();
