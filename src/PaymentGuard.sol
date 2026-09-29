// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

interface IERC20PaymentGuard {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @notice Executes one exact, policy-signed USDC payment from the calling Circle SCA,
///         inside budget limits that were fixed at deployment.
/// @dev Deploy a separate instance per chain/domain. The EIP-712 domain includes chain ID
///      and this contract address; payer is additionally bound to msg.sender.
///
///      Why the limits live here rather than in the calling backend: an agent (or a bug, or
///      a compromised process) that can talk its way past policy in software can still not
///      exceed the budget, because the budget is enforced by the contract holding authority
///      over the transfer. The caps are immutable, so they cannot be raised after deployment
///      either — rotating a budget means deploying a new guard and re-pointing the treasury.
///
///      Amounts are ERC-20 USDC units (6 decimals), the same unit the permit and the permit
///      hash use. That is deliberately NOT Arc's 18-decimal native gas accounting: gas is a
///      separate balance and is never counted against a payment budget.
contract PaymentGuard {
    struct Permit {
        address payer;
        address token;
        address recipient;
        uint256 amount;
        bytes32 evidenceHash;
        bytes32 paymentId;
        uint64 expiry;
    }

    bytes32 private constant EIP712_DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 private constant NAME_HASH = keccak256("ArcPayables");
    bytes32 private constant VERSION_HASH = keccak256("1");
    bytes32 private constant PERMIT_TYPEHASH = keccak256(
        "Permit(address payer,address token,address recipient,uint256 amount,bytes32 evidenceHash,bytes32 paymentId,uint64 expiry)"
    );
    uint256 private constant ARC_TESTNET_CHAIN_ID = 5_042_002;
    uint256 private constant SECP256K1N_HALF =
        0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;
    /// @dev An epoch shorter than this would reset a budget faster than it can be reviewed.
    uint64 private constant MIN_EPOCH_LENGTH = 60;

    address public immutable paymentToken;
    address public immutable policySigner;
    /// @notice Address that may stop and restart payments. It has no access to funds.
    address public immutable pauser;
    /// @notice When true, `pay` reverts. Pausing can only stop payments, never move money.
    bool public paused;

    /// @notice Maximum size of a single payment in USDC units; 0 disables the check.
    uint96 public immutable perPaymentCap;
    /// @notice Maximum total USDC units payable per epoch across all recipients; 0 disables.
    uint96 public immutable epochCap;
    /// @notice Maximum total USDC units payable per epoch to one recipient; 0 disables.
    uint96 public immutable recipientEpochCap;
    /// @notice Length of a budget epoch in seconds; 0 disables all epoch accounting.
    uint64 public immutable epochLength;

    mapping(bytes32 paymentId => bool consumed) public used;
    /// @notice Consumed budget for an epoch, readable by an operator or an agent.
    mapping(uint64 epoch => uint256 spent) public epochSpent;
    mapping(uint64 epoch => mapping(address recipient => uint256 spent)) public recipientEpochSpent;

    error InvalidConfiguration();
    error UnsupportedChain();
    error InvalidPayer();
    error InvalidToken();
    error InvalidRecipient();
    error InvalidPermit();
    error ExpiredPermit();
    error PaymentAlreadyUsed();
    error InvalidSignature();
    error PerPaymentCapExceeded();
    error EpochCapExceeded();
    error RecipientEpochCapExceeded();
    error TokenTransferFailed();
    error NotPauser();
    error Paused();
    error AlreadyPaused();
    error NotPaused();

    event PaymentExecuted(
        bytes32 indexed paymentId,
        bytes32 indexed evidenceHash,
        address indexed payer,
        address recipient,
        address token,
        uint256 amount
    );
    /// @notice The guard stopped or restarted accepting payments.
    event GuardPaused(address indexed pauser);
    event GuardUnpaused(address indexed pauser);
    /// @notice Budget consumption for a settled payment, so the limit is observable off-chain.
    event BudgetConsumed(
        bytes32 indexed paymentId,
        uint64 indexed epoch,
        uint256 epochSpent,
        uint256 recipientEpochSpent
    );

    constructor(
        address token,
        address signer,
        address pauser_,
        uint96 perPaymentCap_,
        uint96 epochCap_,
        uint96 recipientEpochCap_,
        uint64 epochLength_
    ) {
        if (block.chainid != ARC_TESTNET_CHAIN_ID) revert UnsupportedChain();
        if (token == address(0) || signer == address(0) || pauser_ == address(0)) revert InvalidConfiguration();
        // An epoch-based cap without an epoch would silently mean "unlimited", which is the
        // exact failure this contract exists to prevent, so refuse the configuration.
        if (epochLength_ == 0 && (epochCap_ != 0 || recipientEpochCap_ != 0)) {
            revert InvalidConfiguration();
        }
        if (epochLength_ != 0 && epochLength_ < MIN_EPOCH_LENGTH) revert InvalidConfiguration();
        paymentToken = token;
        policySigner = signer;
        pauser = pauser_;
        perPaymentCap = perPaymentCap_;
        epochCap = epochCap_;
        recipientEpochCap = recipientEpochCap_;
        epochLength = epochLength_;
    }

    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH,
                NAME_HASH,
                VERSION_HASH,
                block.chainid,
                address(this)
            )
        );
    }

    function hashPermit(Permit calldata permit) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                PERMIT_TYPEHASH,
                permit.payer,
                permit.token,
                permit.recipient,
                permit.amount,
                permit.evidenceHash,
                permit.paymentId,
                permit.expiry
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator(), structHash));
    }

    /// @notice The epoch a timestamp falls in, or 0 when epoch accounting is disabled.
    function epochAt(uint256 timestamp) public view returns (uint64) {
        if (epochLength == 0) return 0;
        // Casting to 'uint64' is safe because a Unix timestamp divided by at least
        // MIN_EPOCH_LENGTH seconds stays far below 2**64 for any reachable block time.
        // forge-lint: disable-next-line(unsafe-typecast)
        return uint64(timestamp / epochLength);
    }

    /// @notice Budget still available in the current epoch, given a recipient.
    /// @dev Purely informational; `pay` is the authority.
    function remainingEpochBudget(address recipient) external view returns (uint256) {
        if (epochLength == 0) return type(uint256).max;
        uint64 epoch = epochAt(block.timestamp);
        uint256 remaining = type(uint256).max;
        if (epochCap != 0) remaining = epochCap - epochSpent[epoch];
        if (recipientEpochCap != 0) {
            uint256 recipientRemaining = recipientEpochCap - recipientEpochSpent[epoch][recipient];
            if (recipientRemaining < remaining) remaining = recipientRemaining;
        }
        return remaining;
    }

    /// @notice Stop payments. Only the pauser may call this, and only this contract's state changes:
    ///         the pauser cannot move tokens, alter a permit, raise a cap, or spend to itself.
    /// @dev A pause is deliberately not required for safety, because the caps already bound any
    ///      payment. It exists so an incident can be stopped without deploying a new guard.
    function pause() external {
        if (msg.sender != pauser) revert NotPauser();
        if (paused) revert AlreadyPaused();
        paused = true;
        emit GuardPaused(msg.sender);
    }

    /// @notice Resume payments, restoring exactly the same limits the guard was deployed with.
    function unpause() external {
        if (msg.sender != pauser) revert NotPauser();
        if (!paused) revert NotPaused();
        paused = false;
        emit GuardUnpaused(msg.sender);
    }

    function pay(Permit calldata permit, bytes calldata signature) external {
        if (paused) revert Paused();
        if (permit.payer != msg.sender) revert InvalidPayer();
        if (permit.token != paymentToken) revert InvalidToken();
        if (permit.recipient == address(0)) revert InvalidRecipient();
        if (permit.amount == 0 || permit.paymentId == bytes32(0) || permit.evidenceHash == bytes32(0)) {
            revert InvalidPermit();
        }
        // The expiry is a policy bound, not a source of randomness; a few seconds of
        // validator drift on an already-expired permit only makes payment fail closed.
        // forge-lint: disable-next-line(block-timestamp)
        if (block.timestamp >= permit.expiry) revert ExpiredPermit();
        if (used[permit.paymentId]) revert PaymentAlreadyUsed();
        if (_recover(hashPermit(permit), signature) != policySigner) revert InvalidSignature();

        // Budget accounting happens only after the authorization is proven, so an unsigned
        // permit can never consume another payment's budget. A later revert (including a
        // failed transfer) rolls all of this back, so a budget is never spent unsettled.
        uint64 epoch = epochAt(block.timestamp);
        // Only meaningfully populated when epoch accounting is on; the event below is emitted
        // under the same condition, so a disabled guard reports nothing rather than a zero.
        uint256 epochTotal = 0;
        uint256 recipientTotal = 0;
        if (perPaymentCap != 0 && permit.amount > perPaymentCap) revert PerPaymentCapExceeded();
        if (epochLength != 0) {
            epochTotal = epochSpent[epoch] + permit.amount;
            recipientTotal = recipientEpochSpent[epoch][permit.recipient] + permit.amount;
            if (epochCap != 0 && epochTotal > epochCap) revert EpochCapExceeded();
            if (recipientEpochCap != 0 && recipientTotal > recipientEpochCap) {
                revert RecipientEpochCapExceeded();
            }
            epochSpent[epoch] = epochTotal;
            recipientEpochSpent[epoch][permit.recipient] = recipientTotal;
        }

        // CEI: every effect is written before the only external interaction.
        used[permit.paymentId] = true;
        _transferExact(permit.recipient, permit.amount);
        emit PaymentExecuted(
            permit.paymentId,
            permit.evidenceHash,
            permit.payer,
            permit.recipient,
            permit.token,
            permit.amount
        );
        if (epochLength != 0) {
            emit BudgetConsumed(permit.paymentId, epoch, epochTotal, recipientTotal);
        }
    }

    /// @dev Pulls exactly `amount` from this contract's caller. `msg.sender` is preserved
    ///      across internal calls, so the payer remains the calling SCA wallet.
    function _transferExact(address recipient, uint256 amount) private {
        if (!IERC20PaymentGuard(paymentToken).transferFrom(msg.sender, recipient, amount)) {
            revert TokenTransferFailed();
        }
    }

    function _recover(bytes32 digest, bytes calldata signature) private pure returns (address recovered) {
        if (signature.length != 65) revert InvalidSignature();
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly ("memory-safe") {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (uint256(s) > SECP256K1N_HALF || (v != 27 && v != 28)) revert InvalidSignature();
        recovered = ecrecover(digest, v, r, s);
        if (recovered == address(0)) revert InvalidSignature();
    }
}
