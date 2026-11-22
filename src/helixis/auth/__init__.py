"""Authentication primitives Occupella owns.

Supabase owns sessions, passwords and the TOTP protocol; what lives here is
the part it does not provide. Today that is two-factor backup codes — see
`mfa_backup_codes` for what a code actually does, which is not what the name
suggests.
"""
