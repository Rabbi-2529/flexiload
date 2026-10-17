from dataclasses import dataclass


class UnsupportedTopup(ValueError):
    pass


@dataclass(frozen=True)
class UssdDefinition:
    template: str
    dcs: int = 15


USSD_DEFINITIONS = {
    ("grameenphone", "flexiload"): UssdDefinition("*444*0*{number}*{amount}*{pin}#"),
    ("grameenphone", "offer"): UssdDefinition("*444*0*{number}*{amount}*{pin}#"),
    ("grameenphone", "postpaid"): UssdDefinition("*444*0*{number}*{amount}*{pin}#"),
    ("grameenphone", "prepaid"): UssdDefinition("*444*0*{number}*{amount}*{pin}#"),
    ("grameenphone", "regular"): UssdDefinition("*444*0*{number}*{amount}*{pin}#"),
    ("grameenphone", "skitto"): UssdDefinition("*666*{number}*{amount}*{pin}#"),
    ("grameenphone", "topup"): UssdDefinition("*444*0*{number}*{amount}*{pin}#"),
    ("robi", "flexiload"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("robi", "offer"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("robi", "postpaid"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("robi", "prepaid"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("robi", "regular"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("robi", "topup"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("airtel", "flexiload"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("airtel", "offer"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("airtel", "postpaid"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("airtel", "prepaid"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("airtel", "regular"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("airtel", "topup"): UssdDefinition("*888*{number}*{amount}*{pin}#"),
    ("banglalink", "flexiload"): UssdDefinition("*555*{number}*{amount}*{pin}#", 0),
    ("banglalink", "offer"): UssdDefinition("*555*{number}*{amount}*{pin}#", 0),
    ("banglalink", "postpaid"): UssdDefinition("*555*{number}*{amount}*{pin}#", 0),
    ("banglalink", "prepaid"): UssdDefinition("*555*{number}*{amount}*{pin}#", 0),
    ("banglalink", "regular"): UssdDefinition("*555*{number}*{amount}*{pin}#", 0),
    ("banglalink", "topup"): UssdDefinition("*555*{number}*{amount}*{pin}#", 0),
}


def build_ussd(operator, product, number, amount, pin):
    try:
        definition = USSD_DEFINITIONS[(operator, product)]
    except KeyError as exc:
        raise UnsupportedTopup(f"{operator}/{product} is not supported") from exc

    amount_text = format_amount(amount)
    return (
        definition.template.format(
            number=number,
            amount=amount_text,
            pin=pin,
        ),
        definition.dcs,
    )


def format_amount(amount):
    amount_text = format(amount, "f")
    if "." in amount_text:
        amount_text = amount_text.rstrip("0").rstrip(".")
    return amount_text