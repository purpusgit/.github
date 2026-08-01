// Violates Rule 84 step 2: base-URL fallback via defaultValue.
const baseUrl = String.fromEnvironment('BASE_URL', defaultValue: 'https://sandbox.mypurpus.com');
void main() {}
