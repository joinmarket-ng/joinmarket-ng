"""
Mnemonic generation, validation, encryption, and interactive input.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from loguru import logger

# ============================================================================
# Mnemonic Generation and Encryption
# ============================================================================


def generate_mnemonic_secure(word_count: int = 24) -> str:
    """
    Generate a BIP39 mnemonic from secure entropy.

    Args:
        word_count: Number of words (12, 15, 18, 21, or 24)

    Returns:
        BIP39 mnemonic phrase with valid checksum
    """
    from mnemonic import Mnemonic

    if word_count not in (12, 15, 18, 21, 24):
        raise ValueError("word_count must be 12, 15, 18, 21, or 24")

    # Calculate entropy bits: 12 words = 128 bits, 24 words = 256 bits
    # Formula: word_count * 11 = entropy_bits + checksum_bits
    # checksum_bits = entropy_bits / 32
    # So: word_count * 11 = entropy_bits * (1 + 1/32) = entropy_bits * 33/32
    # entropy_bits = word_count * 11 * 32 / 33
    entropy_bits = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}[word_count]

    m = Mnemonic("english")
    return m.generate(strength=entropy_bits)


def validate_mnemonic(mnemonic: str) -> bool:
    """
    Validate a BIP39 mnemonic phrase.

    Args:
        mnemonic: The mnemonic phrase to validate

    Returns:
        True if valid, False otherwise
    """
    from mnemonic import Mnemonic

    m = Mnemonic("english")
    return m.check(mnemonic)


def encrypt_mnemonic(mnemonic: str, password: str) -> bytes:
    """
    Encrypt a mnemonic with a password using Fernet (AES-128-CBC).

    Uses PBKDF2 to derive a key from the password.

    Args:
        mnemonic: The mnemonic phrase to encrypt
        password: The password for encryption

    Returns:
        Encrypted bytes (base64-encoded internally by Fernet)
    """
    import base64

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    # Generate a random salt
    salt = os.urandom(16)

    # Derive a key from password using PBKDF2
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,  # High iteration count for security
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

    # Encrypt the mnemonic
    fernet = Fernet(key)
    encrypted = fernet.encrypt(mnemonic.encode("utf-8"))

    # Prepend salt to encrypted data
    return salt + encrypted


def decrypt_mnemonic(encrypted_data: bytes, password: str) -> str:
    """
    Decrypt a mnemonic with a password.

    Args:
        encrypted_data: The encrypted bytes (salt + Fernet token)
        password: The password for decryption

    Returns:
        The decrypted mnemonic phrase

    Raises:
        ValueError: If decryption fails (wrong password or corrupted data)
    """
    import base64

    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if len(encrypted_data) < 16:
        raise ValueError("Invalid encrypted data")

    # Extract salt and encrypted token
    salt = encrypted_data[:16]
    encrypted_token = encrypted_data[16:]

    # Derive key from password
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

    # Decrypt
    fernet = Fernet(key)
    try:
        decrypted = fernet.decrypt(encrypted_token)
        return decrypted.decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Decryption failed - wrong password or corrupted file") from e
    except UnicodeDecodeError as e:
        raise ValueError(
            "Decrypted content is not valid UTF-8. File may be corrupted or "
            "encrypted with a different tool"
        ) from e


def prompt_password_with_confirmation(max_attempts: int = 3) -> str:
    """
    Prompt for a password with confirmation, retrying on mismatch.

    Args:
        max_attempts: Maximum number of attempts before giving up

    Returns:
        The confirmed password

    Raises:
        typer.Exit: If passwords don't match after max_attempts
    """
    for attempt in range(max_attempts):
        password = typer.prompt("Enter encryption password", hide_input=True)
        confirm = typer.prompt("Confirm password", hide_input=True)
        if password == confirm:
            return password
        remaining = max_attempts - attempt - 1
        if remaining > 0:
            typer.echo(f"Passwords do not match. {remaining} attempt(s) remaining.")
        else:
            logger.error("Passwords do not match after maximum attempts")
            raise typer.Exit(1)
    # Should not reach here, but satisfy type checker
    raise typer.Exit(1)


def save_mnemonic_file(
    mnemonic: str,
    output_file: Path,
    password: str | None = None,
) -> None:
    """
    Save a mnemonic to a file, optionally encrypted.

    Args:
        mnemonic: The mnemonic phrase to save
        output_file: The output file path
        password: Optional password for encryption
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if password:
        encrypted = encrypt_mnemonic(mnemonic, password)
        output_file.write_bytes(encrypted)
        os.chmod(output_file, 0o600)
        logger.info(f"Encrypted mnemonic saved to {output_file}")
    else:
        output_file.write_text(mnemonic)
        os.chmod(output_file, 0o600)
        logger.warning(f"Mnemonic saved to {output_file} (PLAINTEXT - consider using --password)")


def load_mnemonic_file(
    mnemonic_file: Path,
    password: str | None = None,
) -> str:
    """
    Load a mnemonic from a file, decrypting if necessary.

    Args:
        mnemonic_file: Path to the mnemonic file
        password: Password for decryption (required if file is encrypted)

    Returns:
        The mnemonic phrase

    Raises:
        ValueError: If file is encrypted but no password provided
    """
    if not mnemonic_file.exists():
        raise FileNotFoundError(f"Mnemonic file not found: {mnemonic_file}")

    data = mnemonic_file.read_bytes()

    # Try to detect if file is encrypted
    # Encrypted files start with 16-byte salt + Fernet token
    # Plaintext files are ASCII only
    try:
        text = data.decode("utf-8")
        # Check if it looks like a valid mnemonic (words separated by spaces)
        words = text.strip().split()
        if len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words):
            return text.strip()
    except UnicodeDecodeError:
        pass

    # File appears to be encrypted
    if not password:
        raise ValueError(
            "Mnemonic file appears to be encrypted. "
            "Set wallet.mnemonic_password in config or use interactive prompt"
        )

    return decrypt_mnemonic(data, password)


# ============================================================================
# Mnemonic Metadata (wallet birthday / creation height)
# ============================================================================


def _meta_path(mnemonic_file: Path) -> Path:
    """Return the path to the companion metadata file for a mnemonic file.

    The metadata file lives alongside the mnemonic file with a ``.meta``
    suffix appended, e.g. ``default.mnemonic`` -> ``default.mnemonic.meta``.
    """
    return mnemonic_file.with_name(mnemonic_file.name + ".meta")


def save_mnemonic_meta(
    mnemonic_file: Path,
    *,
    creation_height: int | None = None,
) -> None:
    """Persist wallet metadata alongside a mnemonic file.

    The metadata is stored as a small JSON file (``<mnemonic_file>.meta``)
    next to the mnemonic file.  Currently only ``creation_height`` is
    stored, but the format is extensible.

    Args:
        mnemonic_file: Path to the mnemonic file (the .meta suffix is added).
        creation_height: Block height at the time the wallet was created.
    """
    import json

    meta: dict[str, int] = {}
    if creation_height is not None:
        meta["creation_height"] = creation_height

    if not meta:
        return  # Nothing to persist

    path = _meta_path(mnemonic_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2) + "\n")
    os.chmod(path, 0o600)
    logger.debug(f"Saved mnemonic metadata to {path}")


def load_mnemonic_meta(mnemonic_file: Path) -> dict[str, int]:
    """Load wallet metadata from a companion ``.meta`` file.

    Returns an empty dict if the file does not exist (backward-compatible
    with mnemonics created before this feature was added).

    Args:
        mnemonic_file: Path to the mnemonic file.

    Returns:
        Dict with metadata fields (currently only ``creation_height``).
    """
    import json

    path = _meta_path(mnemonic_file)
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
        logger.warning(f"Mnemonic metadata file has unexpected format: {path}")
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Failed to read mnemonic metadata from {path}: {exc}")
        return {}


# ============================================================================
# BIP39 Wordlist and Interactive Mnemonic Input
# ============================================================================


def get_bip39_wordlist() -> list[str]:
    """
    Get the BIP39 English wordlist.

    Returns:
        List of 2048 BIP39 words in order.
    """
    from mnemonic import Mnemonic

    m = Mnemonic("english")
    return list(m.wordlist)


def get_word_completions(prefix: str, wordlist: list[str]) -> list[str]:
    """
    Get BIP39 words that start with the given prefix.

    Args:
        prefix: The prefix to match (case-insensitive)
        wordlist: The BIP39 wordlist

    Returns:
        List of matching words
    """
    prefix_lower = prefix.lower()
    return [w for w in wordlist if w.startswith(prefix_lower)]


def format_word_suggestions(matches: list[str], max_display: int = 8) -> str:
    """
    Format word suggestions for display.

    Args:
        matches: List of matching words
        max_display: Maximum number of words to display

    Returns:
        Formatted suggestion string
    """
    if len(matches) <= max_display:
        return ", ".join(matches)
    return ", ".join(matches[:max_display]) + f", ... (+{len(matches) - max_display} more)"


def _read_char() -> str:
    """Read a single character from stdin without waiting for Enter."""
    import sys
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def _interactive_word_input(
    prompt: str,
    wordlist: list[str],
    max_suggestions: int = 10,
) -> str | None:
    """
    Read a single word with real-time autocomplete suggestions.

    Shows matching words as the user types. Auto-completes when only one match remains.

    Args:
        prompt: The prompt to display (e.g., "Word 1/24: ")
        wordlist: The BIP39 wordlist to match against
        max_suggestions: Show suggestions when matches <= this number

    Returns:
        The completed word, or None if user wants to go back (backspace on empty)

    Raises:
        KeyboardInterrupt: If user presses Ctrl+C
        EOFError: If user presses Ctrl+D
    """
    import sys

    buffer = ""
    suggestion_line = ""

    # Print prompt
    sys.stdout.write(prompt)
    sys.stdout.flush()

    while True:
        ch = _read_char()

        # Handle special characters
        if ch == "\x03":  # Ctrl+C
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt
        elif ch == "\x04":  # Ctrl+D
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise EOFError
        elif ch in ("\r", "\n"):  # Enter
            # Clear suggestion line and move to new line
            if suggestion_line:
                # Clear the suggestion line
                sys.stdout.write(f"\r{prompt}{buffer}" + " " * (len(suggestion_line) + 5))
                sys.stdout.write(f"\r{prompt}{buffer}")
            sys.stdout.write("\n")
            sys.stdout.flush()
            return buffer if buffer else None
        elif ch == "\x7f" or ch == "\x08":  # Backspace
            if buffer:
                buffer = buffer[:-1]
                # Clear current line and suggestion, redraw
                clear_len = len(prompt) + len(buffer) + 1 + len(suggestion_line) + 10
                sys.stdout.write("\r" + " " * clear_len + "\r")
                sys.stdout.write(prompt + buffer)
                sys.stdout.flush()
            else:
                # Backspace on empty buffer - could signal "go back" but we'll ignore
                continue
        elif ch == "\t":  # Tab - try to complete
            if buffer:
                matches = get_word_completions(buffer, wordlist)
                if len(matches) == 1:
                    # Complete the word
                    buffer = matches[0]
                    clear_len = len(prompt) + len(buffer) + len(suggestion_line) + 20
                    sys.stdout.write("\r" + " " * clear_len + "\r")
                    sys.stdout.write(prompt + buffer)
                    sys.stdout.flush()
                elif matches:
                    # Find common prefix
                    common = matches[0]
                    for m in matches[1:]:
                        while not m.startswith(common):
                            common = common[:-1]
                    if len(common) > len(buffer):
                        buffer = common
                        clear_len = len(prompt) + len(buffer) + len(suggestion_line) + 20
                        sys.stdout.write("\r" + " " * clear_len + "\r")
                        sys.stdout.write(prompt + buffer)
                        sys.stdout.flush()
            continue
        elif ch == " ":  # Space - might be pasting multiple words
            if buffer:
                # Treat space as confirming current word and potentially starting paste mode
                # Return current buffer, let caller handle it
                if suggestion_line:
                    sys.stdout.write(f"\r{prompt}{buffer}" + " " * (len(suggestion_line) + 5))
                    sys.stdout.write(f"\r{prompt}{buffer}")
                sys.stdout.write("\n")
                sys.stdout.flush()
                return buffer
            continue
        elif not ch.isalpha():
            # Ignore non-alphabetic characters
            continue
        else:
            # Regular character - add to buffer
            buffer += ch.lower()

        # Get matches for current buffer
        matches = get_word_completions(buffer, wordlist)

        # Auto-complete if exactly one match and buffer is at least 3 chars
        # (to avoid premature completion on short prefixes)
        if len(matches) == 1 and len(buffer) >= 3:
            completed_word = matches[0]
            # Show completion
            clear_len = len(prompt) + len(buffer) + len(suggestion_line) + 20
            sys.stdout.write("\r" + " " * clear_len + "\r")
            sys.stdout.write(prompt + completed_word + "\n")
            sys.stdout.flush()
            return completed_word

        # Update display
        clear_len = len(prompt) + len(buffer) + len(suggestion_line) + 20
        sys.stdout.write("\r" + " " * clear_len + "\r")
        sys.stdout.write(prompt + buffer)

        # Show suggestions if few enough matches
        if buffer and 1 < len(matches) <= max_suggestions:
            suggestion_line = f"  [{', '.join(matches)}]"
            sys.stdout.write(f"\033[90m{suggestion_line}\033[0m")  # Gray color
        elif buffer and len(matches) > max_suggestions:
            suggestion_line = f"  [{len(matches)} matches]"
            sys.stdout.write(f"\033[90m{suggestion_line}\033[0m")
        elif buffer and len(matches) == 0:
            suggestion_line = "  [no match]"
            sys.stdout.write(f"\033[91m{suggestion_line}\033[0m")  # Red color
        else:
            suggestion_line = ""

        sys.stdout.flush()


def _supports_raw_terminal() -> bool:
    """Check if the terminal supports raw character input."""
    import sys

    if not sys.stdin.isatty():
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401

        return True
    except ImportError:
        return False


def interactive_mnemonic_input(word_count: int = 24) -> str:
    """
    Interactively input a BIP39 mnemonic with autocomplete support.

    Features:
    - Real-time suggestions as you type (shows matches when <= 10)
    - Auto-completes when only one word matches (after 3+ chars typed)
    - Tab completion for partial matches
    - Supports pasting all words at once
    - Validates each word against BIP39 wordlist

    Args:
        word_count: Expected number of words (12, 15, 18, 21, or 24)

    Returns:
        The complete mnemonic phrase

    Raises:
        typer.Exit: If user cancels input (Ctrl+C)
    """
    from rich.console import Console

    console = Console()
    wordlist = get_bip39_wordlist()
    words: list[str] = []

    # Check if we can use real-time input
    use_realtime = _supports_raw_terminal()

    # Fallback: set up readline completion if available
    has_readline = False
    if not use_realtime:
        try:
            import readline

            def completer(text: str, state: int) -> str | None:
                matches = get_word_completions(text, wordlist)
                if state < len(matches):
                    return matches[state]
                return None

            readline.set_completer(completer)
            readline.parse_and_bind("tab: complete")
            readline.set_completer_delims(" ")
            has_readline = True
        except ImportError:
            pass

    console.print("\n[bold]Enter your BIP39 mnemonic phrase[/bold]")
    if use_realtime:
        console.print(
            f"[dim]Expected: {word_count} words | Auto-completes | Ctrl+C to cancel[/dim]"
        )
    else:
        console.print(
            f"[dim]Expected: {word_count} words | Tab to autocomplete | Ctrl+C to cancel[/dim]"
        )
    console.print("[dim]Tip: You can paste all words at once[/dim]")
    console.print()

    try:
        while len(words) < word_count:
            word_num = len(words) + 1
            prompt_text = f"Word {word_num}/{word_count}: "

            try:
                if use_realtime:
                    user_input = _interactive_word_input(prompt_text, wordlist)
                    if user_input is None:
                        continue
                    user_input = user_input.strip().lower()
                elif has_readline:
                    user_input = input(prompt_text).strip().lower()
                else:
                    # For terminals without readline, use typer.prompt
                    user_input = (
                        typer.prompt(
                            f"Word {word_num}/{word_count}",
                            prompt_suffix=": ",
                            show_default=False,
                        )
                        .strip()
                        .lower()
                    )
            except EOFError:
                console.print("\n[red]Input cancelled[/red]")
                raise typer.Exit(1)

            if not user_input:
                continue

            # Check if user pasted multiple words at once
            input_parts = user_input.split()
            if len(input_parts) > 1:
                # Validate all pasted words
                all_valid = all(part in wordlist for part in input_parts)
                if all_valid:
                    remaining_slots = word_count - len(words)
                    if len(input_parts) <= remaining_slots:
                        for part in input_parts:
                            words.append(part)
                            console.print(f"  [green]{part}[/green]", highlight=False)
                        continue
                    else:
                        console.print(
                            f"  [red]Too many words: got {len(input_parts)}, "
                            f"only {remaining_slots} remaining[/red]"
                        )
                        continue
                else:
                    # Find which words are invalid
                    invalid_words = [part for part in input_parts if part not in wordlist]
                    console.print(f"  [red]Invalid BIP39 words: {', '.join(invalid_words)}[/red]")
                    continue

            # Check for exact match (single word)
            if user_input in wordlist:
                words.append(user_input)
                # Only print confirmation if not using realtime (realtime already shows it)
                if not use_realtime:
                    console.print(f"  [green]{user_input}[/green]", highlight=False)
                continue

            # Check for prefix matches
            matches = get_word_completions(user_input, wordlist)

            if len(matches) == 0:
                console.print(f"  [red]'{user_input}' - no matching BIP39 word[/red]")
                continue
            elif len(matches) == 1:
                # Auto-complete unique match
                word = matches[0]
                words.append(word)
                if not use_realtime:
                    console.print(
                        f"  [green]{word}[/green] [dim](auto-completed from '{user_input}')[/dim]"
                    )
            else:
                # Show suggestions
                console.print(f"  [yellow]Matches: {format_word_suggestions(matches)}[/yellow]")
                console.print("  [dim]Type more characters to narrow down[/dim]")

    except KeyboardInterrupt:
        console.print("\n[red]Input cancelled[/red]")
        raise typer.Exit(1)
    finally:
        # Restore readline settings if we modified them
        if has_readline:
            try:
                import readline

                readline.set_completer(None)
            except ImportError:
                pass

    mnemonic = " ".join(words)

    # Validate the complete mnemonic
    console.print()
    if validate_mnemonic(mnemonic):
        console.print("[bold green]Mnemonic checksum valid![/bold green]")
    else:
        console.print("[bold red]WARNING: Mnemonic checksum INVALID![/bold red]")
        console.print(
            "[yellow]The words are valid BIP39 words but the checksum doesn't match.[/yellow]"
        )
        console.print("[yellow]This could mean a word was entered incorrectly.[/yellow]")
        if not typer.confirm("Continue anyway?", default=False):
            raise typer.Exit(1)

    return mnemonic
