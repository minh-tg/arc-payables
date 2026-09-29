// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

interface IERC20PaymentGuard {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @notice Executes one exact, policy-signed USDC payment from the calling Circle SCA.
/// @dev Deploy a separate instance per chain/domain. The EIP-712 domain includes chain ID
///      and this contract address; payer is additionally bound to msg.sender.
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
    bytes32 private constant NAME_HASH = keccak256("TameionPaymentGuard");
    bytes32 private constant VERSION_HASH = keccak256("1");
    bytes32 private constant PERMIT_TYPEHASH = keccak256(
        "Permit(address payer,address token,address recipient,uint256 amount,bytes32 evidenceHash,bytes32 paymentId,uint64 expiry)"
    );
    // secp256k1n / 2, rejecting malleable signatures.
    uint256 private constant ARC_TESTNET_CHAIN_ID = 5_042_002;
    uint256 private constant SECP256K1N_HALF =
        0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0;

    address public immutable paymentToken;
    address public immutable policySigner;
    mapping(bytes32 paymentId => bool consumed) public used;

    error InvalidConfiguration();
    error UnsupportedChain();
    error InvalidPayer();
    error InvalidToken();
    error InvalidRecipient();
    error InvalidPermit();
    error ExpiredPermit();
    error PaymentAlreadyUsed();
    error InvalidSignature();
    error TokenTransferFailed();

    event PaymentExecuted(
        bytes32 indexed paymentId,
        bytes32 indexed evidenceHash,
        address indexed payer,
        address recipient,
        address token,
        uint256 amount
    );

    constructor(address token, address signer) {
        if (block.chainid != ARC_TESTNET_CHAIN_ID) revert UnsupportedChain();
        if (token == address(0) || signer == address(0)) revert InvalidConfiguration();
        paymentToken = token;
        policySigner = signer;
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

    function pay(Permit calldata permit, bytes calldata signature) external {
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

        // CEI: the effect is written before the only external interaction. A failing or
        // reverted transfer rolls back this mapping write, so a consumed payment ID is
        // never observable together with an unsettled transfer.
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
