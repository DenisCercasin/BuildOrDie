from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


MAIN_MENU = keyboard(
    (("Find a Book", "m:new"), ("My Requests", "m:requests")),
    (("Active Groups", "m:groups"), ("How It Works", "m:help")),
)
PICKUP = keyboard((("Yes, NTU pickup", "m:pickup"),), (("How It Works", "m:help"),))
FORMAT = keyboard(
    (("Paperback", "f:paperback"), ("Hardcover", "f:hardcover")), (("No preference", "f:any"),)
)
DEADLINE = keyboard(
    (("3 days", "d:3"), ("7 days", "d:7"), ("14 days", "d:14")), (("Enter a date", "d:custom"),)
)
SKIP_BUDGET = keyboard(
    (("No budget limit", "b:skip"),),
)
SKIP_SAVINGS = keyboard(
    (("Any savings", "s:skip"),),
)
REVIEW = keyboard(
    (("Start Searching", "r:confirm"),), (("Edit Preferences", "r:edit"), ("Cancel", "r:cancel"))
)
EDIT = keyboard(
    (("Book", "e:book"), ("Format", "e:format")),
    (("Deadline", "e:deadline"), ("Budget", "e:budget")),
    (("Savings", "e:savings"),),
    (("Back to summary", "e:back"),),
)
SETTINGS = keyboard(
    (("Lower cost", "pref:lower_cost"), ("Earlier pickup", "pref:earlier_delivery")),
    (("Main menu", "m:help"),),
)
INTERRUPT = keyboard(
    (("Finish current request", "i:finish"),),
    (("Discard and start new", "i:discard"), ("Save draft", "i:save")),
)


def request_actions(
    request_id: str, editable: bool = True, searchable: bool = False
) -> InlineKeyboardMarkup:
    rows = []
    if searchable:
        rows.append((("Check store prices", f"q:search:{request_id}"),))
    if editable:
        rows.append(
            (("Edit", f"q:edit:{request_id}"), ("Cancel request", f"q:cancel:{request_id}"))
        )
    else:
        rows.append((("Cancel request", f"q:cancel:{request_id}"),))
    return keyboard(*rows)


def proposal_actions(
    proposal_id: str,
    version: int,
    approval: bool = False,
    decline_label: str = "Decline",
    approved: bool = False,
) -> InlineKeyboardMarkup:
    if approved:
        return keyboard(
            ((decline_label, f"p:decline:{proposal_id}:{version}"),),
            (("View Details", f"p:details:{proposal_id}:{version}"),),
        )
    if approval:
        return keyboard(
            (("Approve This Offer", f"p:approve:{proposal_id}:{version}"),),
            (
                (decline_label, f"p:decline:{proposal_id}:{version}"),
                ("View Details", f"p:details:{proposal_id}:{version}"),
            ),
        )
    return keyboard(
        (("I'm Ready to Buy", f"p:ready:{proposal_id}:{version}"),),
        (
            ("Keep Waiting", f"p:wait:{proposal_id}:{version}"),
            ("View Price Breakdown", f"p:details:{proposal_id}:{version}"),
        ),
    )
