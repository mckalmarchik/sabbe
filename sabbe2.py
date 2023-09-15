import time
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

import constants
import enums
import utils
from logger import logging
from zksync2.core.types import EthBlockParams

from zksync2.module.module_builder import ZkSyncBuilder
from zksync2.manage_contracts.erc20_contract import ERC20Contract


class ContractTypes(enums.AutoEnum):
    UNITROLLER = enums.auto()
    COMPTROLLER = enums.auto()


CONTRACT_ADRESSES = {
    enums.TokenNames.ETH: {
        enums.NetworkNames.zkEra: '0x1BbD33384869b30A323e15868Ce46013C82B86FB',
        enums.NetworkNames.zkEraTestnet: '0x2E147B1243a67073D22A2cEaa8d62912f158AbB9'
    },
    enums.TokenNames.USDC: {
        enums.NetworkNames.zkEra: '0x1181D7BE04D80A8aE096641Ee1A87f7D557c6aeb',
        enums.NetworkNames.zkEraTestnet: '0x602118F0Bd8C56E930dE7f606997B2DB81461735'
    }
}


def enter_market(
    private_key: str,
    network_name: enums.NetworkNames,
    token_name: enums.NetworkNames,
    comptroller_contract,
    proxy: dict[str, str] = None
):
    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    market_address = CONTRACT_ADRESSES[token_name][network_name]

    logging.info(f'[Eralend] Entering market {token_name}')

    txn = comptroller_contract.functions.enterMarkets([market_address]).build_transaction({
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address
    })

    try:
        txn['gas'] = zk_web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logging.critical(f'[Eralend] Insufficient ETH to enter market')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[Eralend] Exception while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[Eralend] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='Eralend'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[Eralend] Successfully entered market {token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[Eralend] Failed to enter market {token_name}')
        return enums.TransactionStatus.FAILED


def supply(
    private_key: str,
    network_name: enums.NetworkNames,
    token_name: enums.NetworkNames,
    *,
    amount: float=None,
    percentage: float=None,
    proxy: dict[str, str]=None
):
    if not any([amount, percentage]):
        raise ValueError('Either amount or percentage must be specified')
    elif all([amount, percentage]):
        raise ValueError('Only one of amount or percentage must be specified')

    if percentage is not None:
        percentage = min(max(percentage, 0.001), 100)

    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    logging.info(f'[Eralend] Supplying {amount} {token_name}')

    market_address = CONTRACT_ADRESSES[token_name][network_name]

    token = constants.NETWORK_TOKENS[network_name, token_name]
    token_contract = ERC20Contract(zk_web3.zksync, token.contract_address, account)

    if token_name in constants.ETH_TOKENS:
        balance = zk_web3.zksync.get_balance(account.address)
    else:
        balance = token_contract.contract.functions.balanceOf(account.address).call()

    if amount is None:
        amount = balance / 10 ** token.decimals * percentage / 100

    logging.info(f'[Eralend] Supplying {amount} {token_name}')

    amount_in_wei = int(amount * 10 ** token.decimals)

    contract_dict = {}

    txn_dict = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address,
        'value': 0
    }

    if token_name in constants.ETH_TOKENS:
        abi_filename = 'CEther.json'
    else:
        abi_filename = 'CErc20.json'
        contract_dict['mintAmount'] = amount_in_wei
        allowance = token_contract.contract.functions.allowance(account.address, market_address).call()
        if allowance < amount_in_wei:
            logging.info(f'[Eralend] Approving {amount} {token_name}')
            approve_txn = token_contract.contract.functions.approve(
                market_address,
                amount_in_wei * 10
            ).build_transaction(txn_dict)
            try:
                approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(approve_txn)
            except Exception as e:
                if 'insufficient funds' in str(e):
                    logging.critical(f'[Eralend] Insufficient ETH to approve {amount} {token_name}')
                    return enums.TransactionStatus.INSUFFICIENT_BALANCE
                logging.error(f'[Eralend] Exception occured while estimating gas: {e}')
                return enums.TransactionStatus.FAILED
            signed_approve = account.sign_transaction(approve_txn)
            approve_tx_hash = zk_web3.eth.send_raw_transaction(signed_approve.rawTransaction)
            logging.info(f'[Eralend] Approve transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')
            approve_receipt = utils.wait_for_transaction_receipt(
                web3=zk_web3.zksync,
                txn_hash=approve_tx_hash,
                logging_prefix='Eralend'
            )

            if approve_receipt and approve_receipt['status'] == 1:
                logging.info(f'[Eralend] Successfully approved {amount} {token_name}')
            else:
                logging.error(f'[Eralend] Failed to approve {amount} {token_name}')
                return enums.TransactionStatus.FAILED
            txn_dict['nonce'] += 1
            while True:
                allowance = token_contract.contract.functions.allowance(
                    account.address, market_address
                ).call()
                if allowance >= amount_in_wei:
                    break
                time.sleep(5)

    with open(Path(__file__).parent / 'abi' / abi_filename) as f:
        market_abi = f.read()

    market_contract = zk_web3.eth.contract(
        address=market_address,
        abi=market_abi
    )

    with open(Path(__file__).parent / 'abi' / 'Comptroller.json') as f:
        comptroller_abi = f.read()

    comptroller_contract = zk_web3.eth.contract(
        address=market_contract.functions.comptroller().call(),
        abi=comptroller_abi
    )

    if not comptroller_contract.functions.checkMembership(account.address, market_address).call():
        enter_market_result = enter_market(private_key, network_name, token_name, comptroller_contract)
        if enter_market_result != enums.TransactionStatus.SUCCESS:
            return enter_market_result
        txn_dict['nonce'] += 1

    txn = market_contract.functions.mint(**contract_dict).build_transaction(txn_dict)

    try:
        txn['gas'] = zk_web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logging.critical(f'[Eralend] Insufficient ETH to supply {amount} {token_name}')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[Eralend] Exception occured while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    if token_name in constants.ETH_TOKENS:
        transaction_fee = txn['gas'] * txn['maxFeePerGas']
        balance = zk_web3.zksync.get_balance(account.address)

        if amount_in_wei + transaction_fee > balance:
            if percentage is None:
                logging.critical(f'[Eralend Supply] Insufficient balance to supply {amount} ETH')
                return enums.TransactionStatus.INSUFFICIENT_BALANCE
            else:
                amount_in_wei = balance - transaction_fee
                if amount_in_wei <= 0:
                    logging.critical(f'[Eralend Supply] Insufficient balance to supply {amount} ETH')
                    return enums.TransactionStatus.INSUFFICIENT_BALANCE

        txn['value'] = amount_in_wei

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[Eralend Supply] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='Eralend'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[Eralend Supply] Successfully supplied {amount} {token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[Eralend Supply] Failed to supply {amount} {token_name}')
        return enums.TransactionStatus.FAILED


def withdraw(
    private_key: str,
    network_name: enums.NetworkNames,
    token_name: enums.NetworkNames,
    percentage: float=100,
    proxy: dict[str, str]=None
):
    percentage = min(max(percentage, 1), 100)

    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    market_address = CONTRACT_ADRESSES[token_name][network_name]

    with open(Path(__file__).parent / 'abi' / 'CEther.json') as f:
        market_abi = f.read()

    market_contract = zk_web3.eth.contract(
        address=market_address,
        abi=market_abi
    )

    balance = market_contract.functions.balanceOfUnderlying(account.address).call()

    amount = int(balance * percentage / 100)
    amount = min(amount, balance)

    logging.info(f'[Eralend Withdraw] Withdrawing {amount} {token_name}')

    txn = market_contract.functions.redeemUnderlying(amount).build_transaction({
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address
    })

    try:
        txn['gas'] = zk_web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logging.critical(f'[Eralend Withdraw] Insufficient ETH to withdraw {amount} {token_name}')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[Eralend Withdraw] Exception occured while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[Eralend Withdraw] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='Eralend'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[Eralend Withdraw] Successfully withdrew {amount} {token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[Eralend Withdraw] Failed to withdraw {amount} {token_name}')
        return enums.TransactionStatus.FAILED


def borrow(
    private_key: str,
    network_name: enums.NetworkNames,
    token_name: enums.NetworkNames,
    percentage: float=80,
    proxy: dict[str, str]=None
):
    percentage = min(max(percentage, 1), 100)

    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    market_address = CONTRACT_ADRESSES[token_name][network_name]

    with open(Path(__file__).parent / 'abi' / 'CEther.json') as f:
        market_abi = f.read()

    market_contract = zk_web3.eth.contract(
        address=market_address,
        abi=market_abi
    )

    with open(Path(__file__).parent / 'abi' / 'Comptroller.json') as f:
        comptroller_abi = f.read()

    comptroller_contract = zk_web3.eth.contract(
        address=market_contract.functions.comptroller().call(),
        abi=comptroller_abi
    )

    total_liquidity = comptroller_contract.functions.getAccountLiquidity(account.address).call()[1]

    token = constants.NETWORK_TOKENS[network_name, token_name]

    amount = int(total_liquidity / 10 ** 18 * 10 ** token.decimals * percentage / 100)

    logging.info(f'[Eralend Borrow] Borrowing {amount} ({percentage}%) {token_name}')

    txn = market_contract.functions.borrow(amount).build_transaction({
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address
    })

    try:
        txn['gas'] = zk_web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logging.critical(f'[Eralend Borrow] Insufficient ETH to borrow {amount} {token_name}')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[Eralend Borrow] Exception occured while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[Eralend Borrow] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='Eralend'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[Eralend Borrow] Successfully borrowed {amount} {token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[Eralend Borrow] Failed to borrow {amount} {token_name}')
        return enums.TransactionStatus.FAILED


def repay(
    private_key: str,
    network_name: enums.NetworkNames,
    token_name: enums.NetworkNames,
    percentage: float=100,
    proxy: dict[str, str]=None
):
    percentage = min(max(percentage, 1), 100)

    network = constants.NETWORKS[network_name]

    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)

    account: LocalAccount = Account.from_key(private_key)

    market_address = CONTRACT_ADRESSES[token_name][network_name]

    with open(Path(__file__).parent / 'abi' / 'CErc20.json') as f:
        market_abi = f.read()

    market_contract = zk_web3.eth.contract(
        address=market_address,
        abi=market_abi
    )

    total_borrowed = market_contract.functions.borrowBalanceCurrent(account.address).call()

    token = constants.NETWORK_TOKENS[network_name, token_name]

    token_contract = ERC20Contract(zk_web3.zksync, token.contract_address, account)

    amount_in_wei = int(total_borrowed * percentage / 100)
    amount = amount_in_wei / 10 ** token.decimals

    logging.info(f'[Eralend Repay] Repaying {amount} ({percentage}%) {token_name}')

    txn_dict = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'from': account.address
    }

    allowance = token_contract.contract.functions.allowance(account.address, market_address).call()
    if allowance < amount_in_wei:
        logging.info(f'[Eralend Repay] Approving {amount} {token_name}')
        approve_txn = token_contract.contract.functions.approve(
            market_address,
            amount_in_wei * 10
        ).build_transaction(txn_dict)
        try:
            approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(approve_txn)
        except Exception as e:
            if 'insufficient funds' in str(e):
                logging.critical(f'[Eralend Repay] Insufficient ETH to approve {amount} {token_name}')
                return enums.TransactionStatus.INSUFFICIENT_BALANCE
            logging.error(f'[Eralend Repay] Exception occured while estimating gas: {e}')
            return enums.TransactionStatus.FAILED
        signed_approve = account.sign_transaction(approve_txn)
        approve_tx_hash = zk_web3.eth.send_raw_transaction(signed_approve.rawTransaction)
        logging.info(f'[Eralend Repay] Approve transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')
        approve_receipt = utils.wait_for_transaction_receipt(
            web3=zk_web3.zksync,
            txn_hash=approve_tx_hash,
            logging_prefix='Eralend Repay'
        )

        if approve_receipt and approve_receipt['status'] == 1:
            logging.info(f'[Eralend Repay] Successfully approved {amount} {token_name}')
        else:
            logging.error(f'[Eralend Repay] Failed to approve {amount} {token_name}')
            return enums.TransactionStatus.FAILED
        txn_dict['nonce'] += 1

        while True:
            allowance = token_contract.contract.functions.allowance(
                account.address, market_address
            ).call()
            if allowance >= amount_in_wei:
                break
            time.sleep(5)

    txn = market_contract.functions.repayBorrow(amount_in_wei).build_transaction(txn_dict)

    try:
        txn['gas'] = zk_web3.eth.estimate_gas(txn)
    except Exception as e:
        if 'insufficient funds' in str(e):
            logging.critical(f'[Eralend Repay] Insufficient ETH to repay {amount} {token_name}')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[Eralend Repay] Exception occured while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = zk_web3.eth.account.sign_transaction(txn, private_key=private_key)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[Eralend Repay] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='Eralend Repay'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[Eralend Repay] Successfully repaid {amount} {token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[Eralend Repay] Failed to repay {amount} {token_name}')
        return enums.TransactionStatus.FAILED
