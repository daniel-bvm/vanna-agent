class DatabaseAuthForm {
    constructor() {
        this.currentSchema = null;
        this.currentDbType = null;
        this.isConnected = false;

        // Database type display names mapping
        this.dbTypeDisplayNames = {
            'postgres': 'PostgreSQL',
            'mysql': 'MySQL',
            'mssql': 'Microsoft SQL Server',
            'sqlite': 'SQLite',
            'duckdb': 'DuckDB',
            'snowflake': 'Snowflake',
            'bigquery': 'Google BigQuery',
            'oracle': 'Oracle',
            'other': 'Other'
        };

        this.initializeElements();
        this.attachEventListeners();
        this.loadSupportedDatabaseTypes();
    }

    initializeElements() {
        this.dbTypeSelect = document.getElementById('db-type');
        this.dynamicForm = document.getElementById('dynamic-form');
        this.formFields = document.getElementById('form-fields');
        this.connectionSection = document.getElementById('connection-section');
        this.connectionToggle = document.getElementById('connection-toggle');
        this.connectionStatusText = document.getElementById('connection-status-text');
        this.statusIndicator = document.getElementById('status-indicator');
        this.loadingElement = document.getElementById('loading');
        this.errorMessage = document.getElementById('error-message');
        this.successMessage = document.getElementById('success-message');
    }

    attachEventListeners() {
        this.dbTypeSelect.addEventListener('change', (e) => {
            this.handleDbTypeChange(e.target.value);
        });

        this.connectionToggle.addEventListener('change', (e) => {
            this.handleConnectionToggle(e.target.checked);
        });
    }

    async loadSupportedDatabaseTypes() {
        this.showLoading('Loading supported database types...');

        try {
            const response = await fetch('./db', {
                method: 'POST'
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }

            const supportedTypes = await response.json();
            this.populateDbTypeDropdown(supportedTypes);
            this.hideMessages();

        } catch (error) {
            this.showError(`Failed to load database types: ${error.message}`);
            console.error('Error loading database types:', error);
        }
    }

    populateDbTypeDropdown(supportedTypes) {
        // Clear existing options except the placeholder
        while (this.dbTypeSelect.children.length > 1) {
            this.dbTypeSelect.removeChild(this.dbTypeSelect.lastChild);
        }

        // Add supported database types as options
        supportedTypes.forEach(dbType => {
            const option = document.createElement('option');
            option.value = dbType;
            option.textContent = this.dbTypeDisplayNames[dbType] || this.formatDbTypeName(dbType);
            this.dbTypeSelect.appendChild(option);
        });
    }

    formatDbTypeName(dbType) {
        // Fallback formatting for unknown database types
        return dbType
            .split('_')
            .map(word => word.charAt(0).toUpperCase() + word.slice(1))
            .join(' ');
    }

    async handleDbTypeChange(dbType) {
        if (!dbType) {
            this.hideForm();
            return;
        }

        this.currentDbType = dbType;
        this.showLoading('Fetching database schema...');

        try {
            const schema = await this.fetchSchema(dbType);
            this.currentSchema = schema;
            this.generateForm(schema);
            this.showForm();
            this.hideMessages();
        } catch (error) {
            this.showError(`Failed to fetch schema: ${error.message}`);
            this.hideForm();
        }
    }

    async fetchSchema(dbType) {
        const response = await fetch(`./db/schemas/${dbType}`);
        if (response.status !== 200) {
            const error = await response.json();
            throw new Error(error.detail || `HTTP ${response.status}`);
        }

        return await response.json();
    }

    generateForm(schema) {
        this.formFields.innerHTML = '';

        if (!schema.properties) {
            throw new Error('Invalid schema format');
        }

        // Skip db_type field as it's already selected
        const fields = Object.entries(schema.properties)
            .filter(([key]) => key !== 'db_type');

        fields.forEach(([fieldName, fieldSchema]) => {
            const fieldElement = this.createFormField(fieldName, fieldSchema);
            this.formFields.appendChild(fieldElement);
        });
    }

    createFormField(fieldName, fieldSchema) {
        const fieldDiv = document.createElement('div');
        fieldDiv.className = 'form-field';

        const label = document.createElement('label');
        label.className = 'form-label';
        label.setAttribute('for', fieldName);
        label.textContent = fieldSchema.title || this.formatFieldName(fieldName);

        const input = this.createInputElement(fieldName, fieldSchema);

        fieldDiv.appendChild(label);
        fieldDiv.appendChild(input);

        if (fieldSchema.description) {
            const description = document.createElement('div');
            description.className = 'form-field-description';
            description.textContent = fieldSchema.description;
            fieldDiv.appendChild(description);
        }

        return fieldDiv;
    }

    createInputElement(fieldName, fieldSchema) {
        const isPassword = fieldName.toLowerCase().includes('password');
        const isOptional = !this.isRequired(fieldName, fieldSchema);

        let input;

        if (fieldSchema.type === 'object' || fieldName === 'connection_params') {
            input = document.createElement('textarea');
            input.className = 'form-textarea';
            input.rows = 4;
            input.placeholder = 'Enter JSON object';
        } else {
            input = document.createElement('input');
            input.className = 'form-input';

            if (isPassword) {
                input.type = 'password';
            } else if (fieldSchema.type === 'integer') {
                input.type = 'number';
                if (fieldSchema.minimum !== undefined) input.min = fieldSchema.minimum;
                if (fieldSchema.maximum !== undefined) input.max = fieldSchema.maximum;
            } else {
                input.type = 'text';
            }
        }

        input.id = fieldName;
        input.name = fieldName;

        if (fieldSchema.default !== undefined && fieldSchema.default !== null) {
            input.value = fieldSchema.default;
        }

        if (!isOptional) {
            input.required = true;
        }

        return input;
    }

    isRequired(fieldName, fieldSchema) {
        // Check if field is in required array or has no default value
        return (this.currentSchema.required && this.currentSchema.required.includes(fieldName)) ||
            (fieldSchema.default === undefined && !fieldName.includes('optional'));
    }

    formatFieldName(fieldName) {
        return fieldName
            .split('_')
            .map(word => word.charAt(0).toUpperCase() + word.slice(1))
            .join(' ');
    }

    async handleConnectionToggle(isConnecting) {
        if (isConnecting) {
            await this.connectDatabase();
        } else {
            await this.disconnectDatabase();
        }
    }

    async connectDatabase() {
        if (!this.validateForm()) {
            this.connectionToggle.checked = false;
            return;
        }

        this.showLoading('Connecting to database...');

        try {
            const formData = this.getFormData();
            const response = await fetch('./v1/connect', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(formData)
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || `HTTP ${response.status}`);
            }

            this.isConnected = true;
            this.updateConnectionStatus(true);

            this.hideMessages();
            // this.showSuccess('Successfully connected to database!');

        } catch (error) {
            this.connectionToggle.checked = false;
            this.showError(`Connection failed: ${error.message}`);
        }
    }

    async disconnectDatabase() {
        this.showLoading('Disconnecting from database...');

        try {
            const response = await fetch('./v1/disconnect', {
                method: 'POST'
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || `HTTP ${response.status}`);
            }

            this.isConnected = false;
            this.updateConnectionStatus(false);
            this.hideMessages();
            // this.showSuccess('Successfully disconnected from database.');

        } catch (error) {
            this.connectionToggle.checked = true;
            this.showError(`Disconnection failed: ${error.message}`);
        }
    }

    validateForm() {
        const formData = this.getFormData();
        const requiredFields = this.currentSchema.required || [];

        for (const field of requiredFields) {
            if (field !== 'db_type' && (!formData[field] || formData[field].toString().trim() === '')) {
                this.showError(`Please fill in the required field: ${this.formatFieldName(field)}`);
                return false;
            }
        }

        return true;
    }

    getFormData() {
        const formData = {
            db_type: this.currentDbType
        };

        const inputs = this.formFields.querySelectorAll('input, textarea, select');
        inputs.forEach(input => {
            let value = input.value;

            // Handle different input types
            if (input.type === 'number') {
                value = value ? parseInt(value) : undefined;
            } else if (input.type === 'checkbox') {
                value = input.checked;
            } else if (input.classList.contains('form-textarea') && value) {
                // Try to parse JSON for textarea fields
                try {
                    value = JSON.parse(value);
                } catch (e) {
                    // If not valid JSON, keep as string
                }
            }

            if (value !== undefined && value !== '') {
                formData[input.name] = value;
            }
        });

        return formData;
    }

    updateConnectionStatus(connected) {
        if (connected) {
            this.connectionStatusText.textContent = 'Connected';
            this.statusIndicator.classList.add('connected');
            // Hide the form fields and db type selection when connected, only show the disconnect switch
            this.dynamicForm.style.display = 'none';
            document.querySelector('.db-type-section').style.display = 'none';
        } else {
            this.connectionStatusText.textContent = 'Disconnected';
            this.statusIndicator.classList.remove('connected');
            // Show the form fields and db type selection again when disconnected
            this.dynamicForm.style.display = 'block';
            document.querySelector('.db-type-section').style.display = 'block';
        }
    }

    showForm() {
        this.dynamicForm.style.display = 'block';
        this.connectionSection.style.display = 'block';
    }

    hideForm() {
        this.dynamicForm.style.display = 'none';
        this.connectionSection.style.display = 'none';
    }

    showLoading(message) {
        this.hideMessages();
        this.loadingElement.style.display = 'flex';
        this.loadingElement.querySelector('span').textContent = message;
    }

    showError(message) {
        this.hideMessages();
        this.errorMessage.style.display = 'block';
        this.errorMessage.textContent = message;
    }

    showSuccess(message) {
        this.hideMessages();
        this.successMessage.style.display = 'block';
        this.successMessage.textContent = message;

        // Auto-hide success message after 3 seconds
        setTimeout(() => {
            this.successMessage.style.display = 'none';
        }, 3000);
    }

    hideMessages() {
        this.loadingElement.style.display = 'none';
        this.errorMessage.style.display = 'none';
        this.successMessage.style.display = 'none';
    }
}

// Initialize the form when the page loads
document.addEventListener('DOMContentLoaded', () => {
    new DatabaseAuthForm();
});