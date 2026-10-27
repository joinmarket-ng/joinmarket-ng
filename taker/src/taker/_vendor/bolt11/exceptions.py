class Bolt11Exception(Exception):
    """Parent Exception"""


class Bolt11NoPaymentHashException(Bolt11Exception):
    """
    MUST include exactly one p field.
    """

    def __init__(self):
        super().__init__("Must include 'payment_hash'")


class Bolt11NoPaymentSecretException(Bolt11Exception):
    """
    MUST include exactly one s field.
    """

    def __init__(self):
        super().__init__("Must include 'payment_secret'")


class Bolt11NoMinFinalCltvException(Bolt11Exception):
    """
    MUST include one c field (min_final_cltv_expiry_delta).
        MUST set c to the minimum cltv_expiry it will accept
        for the last HTLC in the route.
        SHOULD use the minimum data_length possible.
    """

    def __init__(self):
        super().__init__("Should include 'min_final_cltv_expiry_delta'")


class Bolt11InvalidDescriptionHashException(Bolt11Exception):
    """description_hash has to be a valid hex string"""

    def __init__(self):
        super().__init__("description_hash has to be a valid hex string")


class Bolt11DescriptionException(Bolt11Exception):
    """
    MUST include either exactly one d or exactly one h field.
        if d is included:
            MUST set d to a valid UTF-8 string.
            SHOULD use a complete description of the purpose of the payment.
        if h is included:
            MUST make the preimage of the hashed description in h
            available through some unspecified means.
                SHOULD use a complete description of the purpose of the payment.
    """

    def __init__(self):
        super().__init__("Must include either 'description' or 'description_hash', but not both")


class Bolt11NoSignatureException(Bolt11Exception):
    """
    MUST include signature.
    """

    def __init__(self):
        super().__init__("Must include 'signature' or 'private_key'")


class Bolt11SignatureVerifyException(Bolt11Exception):
    """
    If payee is included, signature MUST be valid for payee.
    """

    def __init__(self):
        super().__init__("Included `payee` could not be verified")


class Bolt11SignatureTooShortException(Bolt11Exception):
    """
    Signature is too short.
    """

    def __init__(self):
        super().__init__("Too short to contain signature")


class Bolt11HrpInvalidException(Bolt11Exception):
    """
    Invalid Human Readable Part.
    """

    def __init__(self, message="Human readable part is not valid."):
        super().__init__(message)


class Bolt11AmountInvalidException(Bolt11HrpInvalidException):
    """
    Invalid shortened amount
    """

    def __init__(self):
        super().__init__("Invalid shortened amount.")


class Bolt11Bech32InvalidException(Bolt11Exception):
    """
    Invalid Bech32 string.
    """

    def __init__(self):
        super().__init__("Bech32 string is not valid.")


class Bolt11FeatureException(Exception):
    """
    FeatureExtra has to be bigger than defined Extras
    """
