// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {PaymentGuard} from "../src/PaymentGuard.sol";

interface VmGuardTest {
    function addr(uint256 privateKey) external returns (address);
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function warp(uint256 timestamp) external;
    function chainId(uint256 newChainId) external;
    function expectRevert() external;
    function getBlockTimestamp() external view returns (uint256);
    function prank(address sender) external;
}

contract MockArcUSDC {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 permitted = allowance[from][msg.sender];
        require(permitted >= amount, "allowance");
        require(balanceOf[from] >= amount, "balance");
        allowance[from][msg.sender] = permitted - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract PaymentGuardTest {
    VmGuardTest private constant vm = VmGuardTest(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 private constant POLICY_KEY = 0xA11CE;
    uint256 private constant CHAIN_ID = 5_042_002;
    uint64 private constant DAY = 86_400;
    // Demo budget used by the shared guard. Deliberately small so the caps are real
    // constraints rather than decoration.
    uint96 private constant PER_PAYMENT_CAP = 5_000_000;   // 5 USDC
    uint96 private constant EPOCH_CAP = 20_000_000;        // 20 USDC per day
    uint96 private constant RECIPIENT_EPOCH_CAP = 10_000_000; // 10 USDC per recipient per day

    MockArcUSDC private token;
    PaymentGuard private guard;
    address private policySigner;
    address private recipient = address(0xBEEF);
    address private pauser = address(0x5a5);

    function setUp() public {
        vm.chainId(CHAIN_ID);
        token = new MockArcUSDC();
        policySigner = vm.addr(POLICY_KEY);
        guard = new PaymentGuard(
            address(token), policySigner, pauser, PER_PAYMENT_CAP, EPOCH_CAP, RECIPIENT_EPOCH_CAP, DAY
        );
        token.mint(address(this), 10_000_000);
        token.approve(address(guard), 10_000_000);
    }

    // ---- authorization and replay ------------------------------------------------------

    function test_executesExactAuthorizedPayment() public {
        PaymentGuard.Permit memory permit = _permit(1, recipient, 2_500_000);
        guard.pay(permit, _sign(guard, permit));
        require(token.balanceOf(recipient) == permit.amount, "wrong recipient amount");
        require(token.balanceOf(address(this)) == 7_500_000, "wrong payer balance");
        require(guard.used(permit.paymentId), "payment id not consumed");
    }

    function test_replayIsRejected() public {
        PaymentGuard.Permit memory permit = _permit(2, recipient, 100);
        bytes memory signature = _sign(guard, permit);
        guard.pay(permit, signature);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!success, "replay unexpectedly succeeded");
        require(token.balanceOf(recipient) == 100, "duplicate transfer occurred");
    }

    function test_alteredRecipientAndAmountInvalidateSignature() public {
        PaymentGuard.Permit memory permit = _permit(3, recipient, 100);
        bytes memory signature = _sign(guard, permit);
        permit.recipient = address(0xCAFE);
        (bool changedRecipient,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!changedRecipient, "altered recipient accepted");
        permit.recipient = recipient;
        permit.amount = 101;
        (bool changedAmount,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!changedAmount, "altered amount accepted");
        require(token.balanceOf(recipient) == 0 && token.balanceOf(address(0xCAFE)) == 0, "altered permit transferred funds");
    }

    function test_wrongTokenPayerAndZeroFieldsRejected() public {
        PaymentGuard.Permit memory permit = _permit(4, recipient, 10);
        permit.token = address(0x1234);
        (bool wrongToken,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!wrongToken, "wrong token accepted");

        permit = _permit(5, recipient, 10);
        permit.payer = address(0x1234);
        (bool wrongPayer,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!wrongPayer, "wrong payer accepted");

        permit = _permit(6, address(0), 10);
        (bool zeroRecipient,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!zeroRecipient, "zero recipient accepted");

        permit = _permit(7, recipient, 0);
        (bool zeroAmount,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!zeroAmount, "zero amount accepted");

        permit = _permit(0, recipient, 10);
        (bool zeroPaymentId,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!zeroPaymentId, "zero payment id accepted");
    }

    function test_expiredPermitRejected() public {
        vm.warp(1_800_000_000);
        PaymentGuard.Permit memory permit = _permit(8, recipient, 100);
        permit.expiry = uint64(block.timestamp - 1);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!success, "expired permit accepted");
    }

    function test_wrongChainAndContractDomainRejected() public {
        PaymentGuard.Permit memory permit = _permit(9, recipient, 100);
        bytes memory signature = _sign(guard, permit);
        vm.chainId(1);
        (bool wrongChain,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!wrongChain, "wrong chain accepted");
        vm.chainId(CHAIN_ID);

        PaymentGuard secondGuard = _deployGuard(PER_PAYMENT_CAP, EPOCH_CAP, RECIPIENT_EPOCH_CAP, DAY);
        (bool wrongDomain,) = address(secondGuard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!wrongDomain, "wrong contract domain accepted");
    }

    function test_transferFailureDoesNotConsumePaymentId() public {
        PaymentGuard.Permit memory permit = _permit(10, recipient, 100);
        token.approve(address(guard), 0);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, _sign(guard, permit))));
        require(!success, "payment without allowance succeeded");
        require(!guard.used(permit.paymentId), "failed transfer consumed payment id");
    }

    function test_deploymentIsRestrictedToArcTestnet() public {
        vm.chainId(1);
        (bool success,) = address(this).call(abi.encodeCall(this.deployOnCurrentChain, ()));
        require(!success, "guard deployed off Arc Testnet");
    }

    function deployOnCurrentChain() external returns (address) {
        return address(new PaymentGuard(address(token), policySigner, pauser, PER_PAYMENT_CAP, EPOCH_CAP, RECIPIENT_EPOCH_CAP, DAY));
    }

    function test_wrongSignerRejected() public {
        PaymentGuard.Permit memory permit = _permit(11, recipient, 100);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(uint256(0xB0B), guard.hashPermit(permit));
        bytes memory signature = abi.encodePacked(r, s, v);
        (bool success,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!success, "wrong policy signer accepted");
    }

    // ---- on-chain budget limits --------------------------------------------------------

    function test_perPaymentCapBoundary() public {
        PaymentGuard bounded = _deployGuard(1_000, 0, 0, 0);
        guard = bounded;
        guard.pay(_permit(100, recipient, 1_000), _sign(guard, _permit(100, recipient, 1_000)));
        (bool overCap,) = address(guard).call(
            abi.encodeCall(PaymentGuard.pay, (_permit(101, recipient, 1_001), _sign(guard, _permit(101, recipient, 1_001))))
        );
        require(!overCap, "payment above the per-payment cap was accepted");
        require(token.balanceOf(recipient) == 1_000, "cap boundary transferred the wrong amount");
    }

    function test_epochCapIsExactAcrossRecipients() public {
        PaymentGuard bounded = _deployGuard(10_000, 1_000, 1_000, 3_600);
        guard = bounded;
        _pay(guard, 200, recipient, 500);
        _pay(guard, 201, address(0xF00D), 500);
        require(guard.epochSpent(0) == 1_000, "epoch spend not tracked");
        // A third, never-paid recipient is still refused, so the aggregate cap binds.
        (bool overEpoch,) = address(guard).call(
            abi.encodeCall(PaymentGuard.pay, (_permit(202, address(0xCAFE), 1), _sign(guard, _permit(202, address(0xCAFE), 1))))
        );
        require(!overEpoch, "payment above the epoch cap was accepted");
        require(token.balanceOf(address(0xCAFE)) == 0, "capped payment transferred funds");
    }

    function test_recipientCapIsIndependentPerRecipient() public {
        PaymentGuard bounded = _deployGuard(10_000, 0, 1_000, 3_600);
        guard = bounded;
        _pay(guard, 210, recipient, 1_000);
        (bool overRecipient,) = address(guard).call(
            abi.encodeCall(PaymentGuard.pay, (_permit(211, recipient, 1), _sign(guard, _permit(211, recipient, 1))))
        );
        require(!overRecipient, "payment above the recipient cap was accepted");
        // A different recipient has its own budget.
        _pay(guard, 212, address(0xF00D), 1_000);
        require(token.balanceOf(address(0xF00D)) == 1_000, "second recipient was wrongly blocked");
        require(guard.recipientEpochSpent(0, recipient) == 1_000, "recipient spend not tracked");
    }

    function test_epochRolloverRestoresBudget() public {
        PaymentGuard bounded = _deployGuard(1_000, 1_000, 1_000, 3_600);
        guard = bounded;
        _pay(guard, 220, recipient, 1_000);
        (bool sameEpoch,) = address(guard).call(
            abi.encodeCall(PaymentGuard.pay, (_permit(221, recipient, 1), _sign(guard, _permit(221, recipient, 1))))
        );
        require(!sameEpoch, "budget exceeded within one epoch");
        vm.warp(3_600);
        _pay(guard, 222, recipient, 1_000);
        require(token.balanceOf(recipient) == 2_000, "rollover did not restore budget");
        require(guard.epochAt(3_600) == 1, "epoch index did not advance");
    }

    function test_disabledLimitsMeanNoBudgetEnforcement() public {
        PaymentGuard unbounded = _deployGuard(0, 0, 0, 0);
        token.approve(address(unbounded), type(uint256).max);
        unbounded.pay(_permit(230, recipient, 10_000_000), _sign(unbounded, _permit(230, recipient, 10_000_000)));
        require(token.balanceOf(recipient) == 10_000_000, "unbounded guard refused a payment");
        require(unbounded.remainingEpochBudget(recipient) == type(uint256).max, "unbounded budget not reported");
    }

    function test_epochCapWithoutEpochLengthIsRefusedAtDeployment() public {
        vm.expectRevert();
        new PaymentGuard(address(token), policySigner, pauser, 0, 1_000, 0, 0);
        vm.expectRevert();
        new PaymentGuard(address(token), policySigner, pauser, 0, 0, 1_000, 0);
        vm.expectRevert();
        new PaymentGuard(address(token), policySigner, pauser, 0, 1_000, 1_000, 30);
    }

    function test_capsAreImmutable() public {
        require(guard.perPaymentCap() == PER_PAYMENT_CAP, "per-payment cap changed");
        require(guard.epochCap() == EPOCH_CAP, "epoch cap changed");
        require(guard.recipientEpochCap() == RECIPIENT_EPOCH_CAP, "recipient cap changed");
        require(guard.epochLength() == DAY, "epoch length changed");
    }

    function test_remainingBudgetReflectsTheTightestCap() public {
        PaymentGuard epochOnly = _deployGuard(10_000, 1_000, 0, 3_600);
        require(epochOnly.remainingEpochBudget(recipient) == 1_000, "fresh epoch budget wrong");
        _pay(epochOnly, 240, recipient, 300);
        require(epochOnly.remainingEpochBudget(recipient) == 700, "epoch budget not decremented");

        // A per-recipient cap binds every recipient, including one that has never been paid.
        PaymentGuard bounded = _deployGuard(10_000, 1_000, 400, 3_600);
        guard = bounded;
        require(bounded.remainingEpochBudget(recipient) == 400, "tightest cap not reported");
        _pay(bounded, 241, recipient, 300);
        require(bounded.remainingEpochBudget(recipient) == 100, "recipient budget not decremented");
        require(bounded.remainingEpochBudget(address(0xF00D)) == 400, "untouched recipient budget wrong");
    }

    function test_failedTransferDoesNotConsumeBudget() public {
        PaymentGuard bounded = _deployGuard(1_000, 1_000, 1_000, 3_600);
        guard = bounded;
        token.approve(address(bounded), 0);
        (bool failed,) = address(bounded).call(
            abi.encodeCall(PaymentGuard.pay, (_permit(250, recipient, 1_000), _sign(bounded, _permit(250, recipient, 1_000))))
        );
        require(!failed, "payment without allowance succeeded");
        require(bounded.epochSpent(0) == 0, "failed payment consumed budget");
        require(bounded.recipientEpochSpent(0, recipient) == 0, "failed payment consumed recipient budget");
        // Restore the allowance: the same budget must still be spendable afterwards.
        token.approve(address(bounded), type(uint256).max);
        _pay(bounded, 251, recipient, 1_000);
        require(token.balanceOf(recipient) == 1_000, "budget was lost after a failed transfer");
        require(bounded.epochSpent(0) == 1_000, "budget not consumed by the settled payment");
    }

    function test_unsignedPermitCannotConsumeBudget() public {
        PaymentGuard bounded = _deployGuard(1_000, 1_000, 1_000, 3_600);
        PaymentGuard.Permit memory permit = _permit(260, recipient, 1_000);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(uint256(0xB0B), bounded.hashPermit(permit));
        (bool rejected,) = address(bounded).call(abi.encodeCall(PaymentGuard.pay, (permit, abi.encodePacked(r, s, v))));
        require(!rejected, "wrong signer accepted");
        require(bounded.epochSpent(0) == 0, "unsigned permit consumed budget");
        require(!bounded.used(permit.paymentId), "unsigned permit consumed a payment id");
    }

    // ---- the pause control -------------------------------------------------------------

    function test_a_paused_guard_refuses_payment_and_unpause_restores_it() public {
        PaymentGuard.Permit memory permit = _permit(300, recipient, 1_000);
        bytes memory signature = _sign(guard, permit);

        vm.prank(pauser);
        guard.pause();
        require(guard.paused(), "guard did not report itself paused");
        (bool refused,) = address(guard).call(abi.encodeCall(PaymentGuard.pay, (permit, signature)));
        require(!refused, "a paused guard paid");
        require(token.balanceOf(recipient) == 0, "a paused guard moved funds");
        require(guard.epochSpent(0) == 0, "a paused guard consumed budget");
        require(!guard.used(permit.paymentId), "a paused guard consumed a payment id");

        vm.prank(pauser);
        guard.unpause();
        guard.pay(permit, signature);
        require(token.balanceOf(recipient) == 1_000, "unpause did not restore payments");
    }

    function test_only_the_pauser_may_pause() public {
        PaymentGuard.Permit memory permit = _permit(301, recipient, 1_000);
        // The test contract is the payer but not the pauser.
        (bool notPauser,) = address(guard).call(abi.encodeCall(PaymentGuard.pause, ()));
        require(!notPauser, "a non-pauser paused the guard");
        require(!guard.paused(), "guard paused by a non-pauser");

        // And the pauser cannot be impersonated after the fact.
        vm.prank(pauser);
        guard.pause();
        (bool doublePause,) = address(this).call(abi.encodeCall(this.pauseAsPauser, ()));
        require(!doublePause, "paused twice");
        (bool notPauserUnpause,) = address(guard).call(abi.encodeCall(PaymentGuard.unpause, ()));
        require(!notPauserUnpause, "a non-pauser unpaused the guard");
        // The permit is still spendable once the real pauser resumes.
        vm.prank(pauser);
        guard.unpause();
        guard.pay(permit, _sign(guard, permit));
    }

    function pauseAsPauser() external {
        vm.prank(pauser);
        guard.pause();
    }

    function test_pausing_cannot_move_funds_or_change_the_limits() public {
        uint256 payerBefore = token.balanceOf(address(this));
        uint256 recipientBefore = token.balanceOf(recipient);
        uint96 perPaymentBefore = guard.perPaymentCap();
        uint96 epochBefore = guard.epochCap();

        vm.prank(pauser);
        guard.pause();

        require(token.balanceOf(address(this)) == payerBefore, "pause moved payer funds");
        require(token.balanceOf(recipient) == recipientBefore, "pause moved recipient funds");
        require(guard.perPaymentCap() == perPaymentBefore, "pause changed a cap");
        require(guard.epochCap() == epochBefore, "pause changed the epoch cap");
        require(guard.policySigner() == policySigner, "pause changed the signer");
    }

    function test_a_guard_cannot_be_deployed_without_a_pauser() public {
        vm.expectRevert();
        new PaymentGuard(address(token), policySigner, address(0), PER_PAYMENT_CAP, EPOCH_CAP, RECIPIENT_EPOCH_CAP, DAY);
    }

    // ---- helpers ----------------------------------------------------------------------

    function _deployGuard(uint96 perPayment, uint96 epochCap_, uint96 recipientCap, uint64 epochLen)
        private
        returns (PaymentGuard deployed)
    {
        deployed = new PaymentGuard(address(token), policySigner, pauser, perPayment, epochCap_, recipientCap, epochLen);
        token.approve(address(deployed), type(uint256).max);
        return deployed;
    }

    function _pay(PaymentGuard target, uint256 nonce, address to, uint256 amount) private {
        PaymentGuard.Permit memory permit = _permit(nonce, to, amount);
        target.pay(permit, _sign(target, permit));
    }

    function _permit(uint256 nonce, address to, uint256 amount) private view returns (PaymentGuard.Permit memory) {
        return PaymentGuard.Permit({
            payer: address(this),
            token: address(token),
            recipient: to,
            amount: amount,
            evidenceHash: keccak256(abi.encode("evidence", nonce)),
            paymentId: bytes32(nonce),
            expiry: uint64(vm.getBlockTimestamp() + 300)
        });
    }

    function _sign(PaymentGuard target, PaymentGuard.Permit memory permit) private returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(POLICY_KEY, target.hashPermit(permit));
        return abi.encodePacked(r, s, v);
    }
}
