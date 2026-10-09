from decimal import Decimal

from bookpool_bot.models import (
    BookRequest,
    BookRequestInput,
    GroupProposal,
    ProposalStatus,
    RequestStatus,
)
from bookpool_bot.utils import SGT, money, now

STATUS_LABEL = {
    RequestStatus.SEARCHING: "Searching for offers",
    RequestStatus.WAITING: "Waiting for a compatible group",
    RequestStatus.GROUP_AVAILABLE: "Group offer available",
    RequestStatus.AWAITING_APPROVAL: "Awaiting final approval",
    RequestStatus.PAYMENT_PENDING: "Payment pending",
    RequestStatus.ORDER_PLACED: "Order placed",
    RequestStatus.CANCELLED: "Cancelled",
    RequestStatus.EXPIRED: "Expired",
}


def request_summary(request: BookRequestInput) -> str:
    savings = (
        f"Minimum desired savings: {request.minimum_savings_percent}%"
        + (f" and {money(request.minimum_savings_minor)}" if request.minimum_savings_minor else "")
        if request.minimum_savings_percent is not None
        else f"Minimum desired savings: {money(request.minimum_savings_minor)}"
        + (" (default)" if request.minimum_savings_minor == 0 else "")
    )
    lines = [
        "📚 Your Book Request",
        "",
        f"Book: {request.title or request.isbn}",
        f"Author: {request.author or 'Not specified'}",
    ]
    if request.edition:
        lines.append(f"Edition: {request.edition}")
    lines.extend(
        [
            f"Language: {request.language}",
            f"Format: {request.format.value.title()}",
            f"Maximum total budget: {money(request.maximum_budget_minor) if request.maximum_budget_minor else 'No limit set'}",
            f"Latest pickup: {request.latest_delivery_date.isoformat()} (Singapore)",
            f"Priority: {'Earlier pickup' if request.preference == 'earlier_delivery' else 'Lower cost (default)'}",
            savings,
            "Pickup: NTU",
            "",
            "Ready to search? Prices and availability have not been checked yet.",
        ]
    )
    return "\n".join(lines)


def request_line(request: BookRequest) -> str:
    return f"{request.title or request.isbn} — {STATUS_LABEL[request.status]}\nID: {request.request_id}\nLatest pickup: {request.latest_delivery_date.isoformat()}"


def proposal_text(proposal: GroupProposal, title: str = "Your book") -> str:
    if proposal.book_titles:
        title = ", ".join(proposal.book_titles)
    valid = (
        proposal.status not in {ProposalStatus.EXPIRED, ProposalStatus.SUPERSEDED}
        and proposal.expires_at > now()
    )
    savings_percent = (
        Decimal(proposal.savings_minor * 100) / Decimal(proposal.individual_total_minor)
        if proposal.individual_total_minor
        else Decimal(0)
    )
    lines = [
        f"📖 {title}",
        "",
        "Estimated quote" if proposal.is_estimate else "Confirmed quote",
        f"Merchant: {proposal.merchant_name}",
        f"Buying alone: {money(proposal.individual_total_minor)}",
        f"Buying with group: {money(proposal.group_total_minor)}",
        f"Your savings: {money(proposal.savings_minor)} ({savings_percent:.1f}%)"
        if proposal.savings_minor >= 0
        else f"Group costs {money(-proposal.savings_minor)} more than buying alone.",
        f"Group: {proposal.participant_count} buyers",
        f"Free shipping: {'Unlocked' if proposal.free_shipping_unlocked else 'Not unlocked' if proposal.free_shipping_unlocked is False else 'Not specified'}",
        f"Estimated pickup: {proposal.estimated_delivery_date.isoformat()}",
        f"Quote version: {proposal.quote_version}",
        f"Valid until: {proposal.expires_at.astimezone(SGT).isoformat(timespec='minutes')}",
    ]
    if proposal.recommendation_reason:
        lines.extend(["", proposal.recommendation_reason])
    if proposal.user_approved:
        lines.extend(["", "You approved this quote version."])
    if not valid:
        lines.extend(["", "This quote is no longer available. Please refresh offers."])
    return "\n".join(lines)


def proposal_details(proposal: GroupProposal, title: str) -> str:
    if all(
        v is not None
        for v in (proposal.item_price_minor, proposal.shipping_minor, proposal.other_fees_minor)
    ):
        breakdown = [
            f"Book: {money(proposal.item_price_minor)}",
            f"Shipping: {money(proposal.shipping_minor)}",
            f"Other fees: {money(proposal.other_fees_minor)}",
        ]
    else:
        breakdown = ["Item and shipping split: Not supplied by backend"]
    return "\n".join(
        [
            proposal_text(proposal, title),
            "",
            "Your group price breakdown:",
            *breakdown,
            f"Total: {money(proposal.group_total_minor)}",
            f"Payment status: {proposal.payment_status.replace('_', ' ').title()}",
            "",
            "Approval records consent to this quote version only. It does not charge you or place the order.",
        ]
    )
