/**
 * VANILLA JAVASCRIPT API CLIENT (static/core/js/api.js)
 * Modular asynchronous API client utility for interacting with Django RESTful API endpoints.
 * Automatically injects Django CSRF tokens, sets headers, handles promise resolution, 
 * standardizes error trapping, and displays interactive floating toast notifications.
 */

class APIClient {
    /**
     * Extracts named cookie value (specifically 'csrftoken') from document.cookie.
     */
    static getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    /**
     * Generic asynchronous HTTP request wrapper around native browser fetch().
     */
    static async request(url, options = {}) {
        const csrfToken = this.getCookie('csrftoken');
        const defaultHeaders = {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        };

        if (csrfToken) {
            defaultHeaders['X-CSRFToken'] = csrfToken;
        }

        options.headers = { ...defaultHeaders, ...options.headers };

        try {
            const response = await fetch(url, options);
            let json;
            try {
                json = await response.json();
            } catch (e) {
                json = { status: 'error', message: `HTTP ${response.status}: ${response.statusText}` };
            }

            if (!response.ok || json.status === 'error') {
                const errorMsg = json.message || `Request failed with status ${response.status}`;
                this.showToast(errorMsg, 'error');
                throw new Error(errorMsg);
            }

            return json.data;
        } catch (err) {
            console.error('[APIClient Request Error]:', err.message);
            throw err;
        }
    }

    /** HTTP GET helper method */
    static get(url) {
        return this.request(url, { method: 'GET' });
    }

    /** HTTP POST helper method */
    static post(url, data) {
        return this.request(url, {
            method: 'POST',
            body: JSON.stringify(data)
        });
    }

    /**
     * Renders floating alert toast notifications dynamically.
     */
    static showToast(message, type = 'info') {
        let container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            container.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 9999;
                display: flex;
                flex-direction: column;
                gap: 10px;
                max-width: 380px;
            `;
            document.body.appendChild(container);
        }

        const toast = document.createElement('div');
        const bgColors = {
            error: '#ef4444',
            success: '#10b981',
            warning: '#f59e0b',
            info: '#2563eb'
        };
        const color = bgColors[type] || bgColors.info;

        toast.style.cssText = `
            background-color: ${color};
            color: white;
            padding: 12px 18px;
            border-radius: 8px;
            font-family: 'Inter', system-ui, sans-serif;
            font-size: 13px;
            font-weight: 500;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            opacity: 0;
            transform: translateY(-10px);
            transition: all 0.3s ease;
        `;
        toast.innerText = message;
        container.appendChild(toast);

        // Animation in
        requestAnimationFrame(() => {
            toast.style.opacity = '1';
            toast.style.transform = 'translateY(0)';
        });

        // Auto remove after 4 seconds
        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(-10px)';
            setTimeout(() => toast.remove(), 300);
        }, 4000);
    }
}
