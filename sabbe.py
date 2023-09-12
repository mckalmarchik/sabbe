from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

import utils
from logger import logging
from zksync2.core.types import EthBlockParams

from zksync2.module.module_builder import ZkSyncBuilder
from zksync2.manage_contracts.erc20_contract import ERC20Contract
import eth_abi
import enums
import constants


ZERO_ADDRESS = '0x0000000000000000000000000000000000000000'


class ContractTypes(enums.AutoEnum):
    POOL_FACTORY = enums.auto()
    SWAP = enums.auto()

CONTRACT_ADRESSES = {
    ContractTypes.POOL_FACTORY: {
        enums.NetworkNames.zkEra: '0xf2DAd89f2788a8CD54625C60b55cD3d2D0ACa7Cb',
        enums.NetworkNames.zkEraTestnet: '0xf2FD2bc2fBC12842aAb6FbB8b1159a6a83E72006',
    },
    ContractTypes.SWAP: {
        enums.NetworkNames.zkEra: '0x2da10A1e27bF85cEdD8FFb1AbBe97e53391C0295',
        enums.NetworkNames.zkEraTestnet: '0xB3b7fCbb8Db37bC6f572634299A58f51622A847e',
    }
}


def get_pool_contract(
    zk_web3: Web3,
    network_name: enums.NetworkNames,
    account: LocalAccount,
    first_token_address: str,
    second_token_address: str
):
    with open(Path(__file__).parent / 'abi' / 'SyncSwapClassicPoolFactory.json') as file:
        pool_factory_abi = file.read()

    pool_factory_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.POOL_FACTORY][network_name],
        abi=pool_factory_abi
    )

    pool_address = pool_factory_contract.functions.getPool(
        first_token_address,
        second_token_address
    ).call()

    with open(
        Path(__file__).parent / 'abi' / 'SyncSwapClassicPool.json') as file:
        pool_abi = file.read()

    pool_contract = zk_web3.eth.contract(
        address=pool_address,
        abi=pool_abi
    )

    return pool_contract


def swap(
    private_key: str,
    network_name: enums.NetworkNames,
    from_token_name: enums.TokenNames,
    to_token_name: enums.TokenNames,
    slippage: float,
    *,
    amount: float = None,
    percentage: float = None,
    proxy: dict[str, str] = None
):
    if not any([amount, percentage]):
        raise ValueError('Either amount or percentage must be specified')
    elif all([amount, percentage]):
        raise ValueError('Only one of amount or percentage must be specified')

    network = constants.NETWORKS[network_name]
    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)
    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'SyncSwapRouter.json') as file:
        swap_router_abi = file.read()

    swap_router_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.SWAP][network_name],
        abi=swap_router_abi
    )

    weth_address = swap_router_contract.functions.wETH().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if from_token_name in constants.ETH_TOKENS:
        from_token_address = weth_address
        from_token_contract = weth_contract
        from_token_decimals = weth_decimals
        balance_in_wei = zk_web3.zksync.get_balance(account.address)
    else:
        from_token = constants.NETWORK_TOKENS[network_name, from_token_name]
        from_token_address = from_token.contract_address
        from_token_contract = ERC20Contract(zk_web3.zksync, from_token_address, account)
        from_token_decimals = from_token.decimals
        balance_in_wei = from_token_contract.contract.functions.balanceOf(
            account.address
        ).call()

    if to_token_name in constants.ETH_TOKENS:
        to_token_address = weth_address
        to_token_contract = weth_contract
        to_token_decimals = weth_decimals
    else:
        to_token = constants.NETWORK_TOKENS[network_name, to_token_name]
        to_token_address = to_token.contract_address
        to_token_contract = ERC20Contract(zk_web3.zksync, to_token_address, account)
        to_token_decimals = to_token.decimals

    if amount is None:
        if percentage == 100:
            amount_in_wei = balance_in_wei
        else:
            amount_in_wei = int(balance_in_wei * percentage / 100)
        amount = amount_in_wei / 10 ** from_token_decimals
    else:
        amount_in_wei = int(amount * 10 ** from_token_decimals)

    logging.info(f'[SyncSwap] Swapping {amount} {from_token_name} to {to_token_name}')

    pool_contract = get_pool_contract(
        zk_web3,
        network_name,
        account,
        from_token_address,
        to_token_address
    )

    try:
        reserves = pool_contract.functions.getReserves().call()
    except Exception as e:
        logging.error(f'[SyncSwap] Failed to get pool info')
        return enums.TransactionStatus.FAILED

    if int(from_token_address, 16) < int(to_token_address, 16):
        reserve_first, reserve_second = reserves
    else:
        reserve_first, reserve_second = reversed(reserves)

    amount_out_min = int(
        (reserve_second / 10 ** to_token_decimals)
        / (reserve_first / 10 ** from_token_decimals)
        * amount
        * 10 ** to_token_decimals
    )

    amount_out_min = int(amount_out_min * (1 - slippage / 100))

    if reserve_first < amount_in_wei or reserve_second < amount_out_min:
        logging.error('[SyncSwap] Insufficient liquidity in the pool')
        return enums.TransactionStatus.INSUFFICIENT_LIQUIDITY

    withdraw_mode = 1

    swap_data = eth_abi.encode(
        ['address', 'address', 'uint8'],
        [from_token_address, account.address, withdraw_mode]
    )

    steps = [
        (
            pool_contract.address,
            swap_data,
            ZERO_ADDRESS,
            b'0x'
        )
    ]

    native_eth_address = ZERO_ADDRESS

    paths = [
        (
            steps,
            ZERO_ADDRESS if from_token_name in constants.ETH_TOKENS else from_token_address,
            amount_in_wei
        )
    ]

    deadline = zk_web3.eth.get_block('latest')['timestamp'] + 1800

    burn_liquidty_data = eth_abi.encode(
        ['address', 'address', 'uint8'],
        [from_token_address, account.address, withdraw_mode]
    )

    txn_data = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'from': account.address,
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'value': 0,
        'gas': 0
    }

    if from_token_name in constants.ETH_TOKENS:
        txn_data['value'] = amount_in_wei
    else:
        allowance = from_token_contract.contract.functions.allowance(
            account.address,
            swap_router_contract.address
        ).call()

        if allowance < amount_in_wei:
            approve_amount_in_wei = amount_in_wei * 10
            approve_amount = amount * 10
            logging.info(f'[SyncSwap] Approving {approve_amount} {from_token_name}')
            approve_txn = from_token_contract.contract.functions.approve(
                swap_router_contract.address,
                approve_amount_in_wei
            ).build_transaction(txn_data)
            try:
                approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(approve_txn)
            except Exception as e:
                if 'insufficient balance' in str(e):
                    logging.critical(f'[SyncSwap] Insufficient balance to approve {from_token_name}')
                    return enums.TransactionStatus.INSUFFICIENT_BALANCE
                logging.error(f'[SyncSwap] Error while estimating gas: {e}')
                return enums.TransactionStatus.FAILED
            signed_approve = account.sign_transaction(approve_txn)
            approve_tx_hash = zk_web3.eth.send_raw_transaction(
                signed_approve.rawTransaction
            )
            logging.info(f'[SyncSwap] Approve Transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')
            approve_receipt = utils.wait_for_transaction_receipt(
                web3=zk_web3.zksync,
                txn_hash=approve_tx_hash,
                logging_prefix='SyncSwap'
            )

            if approve_receipt and approve_receipt['status'] == 1:
                logging.info(f'[SyncSwap] Successfully approved {approve_amount} {from_token_name}')
            else:
                logging.error(f'[SyncSwap] Failed to approve {approve_amount} {from_token_name}')
                return enums.TransactionStatus.FAILED
            txn_data['nonce'] += 1
            utils.random_sleep()

    txn = swap_router_contract.functions.swap(
        paths,
        amount_out_min,
        deadline
    ).build_transaction(txn_data)

    try:
        txn['gas'] = zk_web3.zksync.eth_estimate_gas(txn)
    except Exception as e:
        if 'insufficient balance' in str(e):
            logging.critical(f'[SyncSwap] Insufficient balance to swap {from_token_name} to {to_token_name}')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[SyncSwap] Error while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = account.sign_transaction(txn)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[SyncSwap] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='SyncSwap'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[SyncSwap] Successfully swapped {amount} {from_token_name} to {to_token_name}')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[SyncSwap] Failed to swap {amount} {from_token_name} to {to_token_name}')
        return enums.TransactionStatus.FAILED

def add_liquidity(
    private_key: str,
    network_name: enums.NetworkNames,
    first_token_name: enums.TokenNames,
    second_token_name: enums.TokenNames,
    *,
    amount: float = None,
    percentage: float = None,
    proxy: dict[str, str] = None
):
    if not any([amount, percentage]):
        raise ValueError('Either amount or percentage must be specified')
    elif all([amount, percentage]):
        raise ValueError('Only one of amount or percentage must be specified')

    network = constants.NETWORKS[network_name]
    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)
    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'SyncSwapRouter.json') as file:
        swap_router_abi = file.read()

    swap_router_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.SWAP][network_name],
        abi=swap_router_abi
    )

    weth_address = swap_router_contract.functions.wETH().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if first_token_name in constants.ETH_TOKENS:
        first_token_address = weth_address
        first_token_contract = weth_contract
        first_token_decimals = weth_decimals
        balance_in_wei = zk_web3.zksync.get_balance(account.address)
    else:
        first_token = constants.NETWORK_TOKENS[network_name, first_token_name]
        first_token_address = first_token.contract_address
        first_token_contract = ERC20Contract(zk_web3.zksync, first_token_address, account)
        first_token_decimals = first_token.decimals
        balance_in_wei = first_token_contract.contract.functions.balanceOf(
            account.address
        ).call()

    if second_token_name in constants.ETH_TOKENS:
        second_token_address = weth_address
    else:
        second_token = constants.NETWORK_TOKENS[network_name, second_token_name]
        second_token_address = second_token.contract_address


    if amount is None:
        if percentage == 100:
            amount_in_wei = balance_in_wei
        else:
            amount_in_wei = int(balance_in_wei * percentage / 100)
        amount = amount_in_wei / 10 ** first_token_decimals
    else:
        amount_in_wei = int(amount * 10 ** first_token_decimals)

    logging.info(f'[SyncSwap] Adding {amount} {first_token_name} to {first_token_name}/{second_token_name} liquidity pool')

    pool_contract = get_pool_contract(
        zk_web3,
        network_name,
        account,
        first_token_address,
        second_token_address
    )

    contract_data = {
        'pool': pool_contract.address,
        'inputs': [
            {
                'token': ZERO_ADDRESS if first_token_name in constants.ETH_TOKENS else first_token_address,
                'amount': amount_in_wei
            },
            {
                'token': second_token_address,
                'amount': 0
            }
        ],
        'data': eth_abi.encode(['address'], [account.address]),
        'minLiquidity': 0,
        'callback': ZERO_ADDRESS,
        'callbackData': b''
    }

    txn_data = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'from': account.address,
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'gas': 0,
        'value': 0
    }

    if first_token_name in constants.ETH_TOKENS:
        txn_data['value'] = amount_in_wei
    else:
        allowance = first_token_contract.contract.functions.allowance(
            account.address,
            swap_router_contract.address
        ).call()

        if allowance < amount_in_wei:
            logging.info(f'[SyncSwap] Approving {amount} {first_token_name}')
            approve_amount = amount_in_wei * 10
            approve_txn = first_token_contract.contract.functions.approve(
                swap_router_contract.address,
                amount_in_wei * 10
            ).build_transaction(txn_data)
            try:
                approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(approve_txn)
            except Exception as e:
                if 'insufficient balance' in str(e):
                    logging.critical(f'[SyncSwap] Insufficient balance to approve {first_token_name}')
                    return enums.TransactionStatus.INSUFFICIENT_BALANCE
                logging.error(f'[SyncSwap] Error while estimating gas: {e}')
                return enums.TransactionStatus.FAILED
            signed_approve = account.sign_transaction(approve_txn)
            approve_tx_hash = zk_web3.eth.send_raw_transaction(signed_approve.rawTransaction)
            logging.info(f'[SyncSwap] Approve Transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')
            approve_receipt = utils.wait_for_transaction_receipt(
                web3=zk_web3.zksync,
                txn_hash=approve_tx_hash,
                logging_prefix='SyncSwap'
            )

            if approve_receipt and approve_receipt['status'] == 1:
                logging.info(f'[SyncSwap] Successfully approved {approve_amount / 10 ** first_token_decimals} {first_token_name}')
            else:
                logging.error(f'[SyncSwap] Failed to approve {approve_amount / 10 ** first_token_decimals} {first_token_name}')
                return enums.TransactionStatus.FAILED
            txn_data['nonce'] += 1
            utils.random_sleep()

    txn = swap_router_contract.functions.addLiquidity2(
        **contract_data
    ).build_transaction(txn_data)

    try:
        txn['gas'] = zk_web3.zksync.eth_estimate_gas(txn)
    except Exception as e:
        if 'insufficient balance' in str(e):
            logging.critical(f'[SyncSwap] Insufficient balance to add liquidity')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[SyncSwap] Error while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed_txn = account.sign_transaction(txn)

    txn_hash = zk_web3.eth.send_raw_transaction(signed_txn.rawTransaction)

    logging.info(f'[SyncSwap] Transaction: {network.txn_explorer_url}{txn_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=txn_hash,
        logging_prefix='SyncSwap'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[SyncSwap] Successfully added {amount} {first_token_name} to {first_token_name}/{second_token_name} liquidity pool')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[SyncSwap] Failed to add {amount} {first_token_name} to {first_token_name}/{second_token_name} liquidity pool')
        return enums.TransactionStatus.FAILED


def burn_liquidity(
    private_key: str,
    network_name: enums.NetworkNames,
    first_token_name: enums.TokenNames,
    second_token_name: enums.TokenNames,
    *,
    percentage: float = 100,
    proxy: dict[str, str] = None
):
    network = constants.NETWORKS[network_name]
    zk_web3 = ZkSyncBuilder.build(network.rpc_url, proxy=proxy)
    account: LocalAccount = Account.from_key(private_key)

    with open(Path(__file__).parent / 'abi' / 'SyncSwapRouter.json') as file:
        swap_router_abi = file.read()

    swap_router_contract = zk_web3.eth.contract(
        address=CONTRACT_ADRESSES[ContractTypes.SWAP][network_name],
        abi=swap_router_abi
    )

    weth_address = swap_router_contract.functions.wETH().call()
    weth_contract = ERC20Contract(zk_web3.zksync, weth_address, account)
    weth_decimals = weth_contract.contract.functions.decimals().call()

    if first_token_name in constants.ETH_TOKENS:
        first_token_address = weth_address
        first_token_contract = weth_contract
        first_token_decimals = weth_decimals
    else:
        first_token = constants.NETWORK_TOKENS[network_name, first_token_name]
        first_token_address = first_token.contract_address
        first_token_contract = ERC20Contract(zk_web3.zksync, first_token_address, account)
        first_token_decimals = first_token.decimals

    if second_token_name in constants.ETH_TOKENS:
        second_token_address = weth_address
    else:
        second_token = constants.NETWORK_TOKENS[network_name, second_token_name]
        second_token_address = second_token.contract_address

    logging.info(f'[SyncSwap] Remmoving {percentage}% liquidity of {first_token_name}/{second_token_name} liquidity pool')

    pool_contract = get_pool_contract(
        zk_web3,
        network_name,
        account,
        first_token_address,
        second_token_address
    )

    balance_in_wei = int(pool_contract.functions.balanceOf(
        account.address
    ).call())

    if percentage == 100:
        amount_in_wei = balance_in_wei
    else:
        amount_in_wei = int(balance_in_wei * percentage / 100)

    amount = amount_in_wei / 10 ** pool_contract.functions.decimals().call()

    withdraw_mode = 1

    burn_liquidty_data = eth_abi.encode(
        ['address', 'address', 'uint8'],
        [first_token_address, account.address, withdraw_mode]
    )

    txn_data = {
        'nonce': zk_web3.zksync.get_transaction_count(account.address, EthBlockParams.LATEST.value),
        'from': account.address,
        'maxPriorityFeePerGas': 100_000_000,
        'maxFeePerGas': zk_web3.zksync.gas_price,
        'value': 0,
        'gas': 0
    }

    if pool_contract.functions.allowance(account.address, swap_router_contract.address).call() < amount_in_wei:
        approve_amount_in_wei = amount_in_wei * 10
        logging.info(f'[SyncSwap] Approving {amount * 10} pool tokens to SyncSwapRouter contract')

        approve_txn = pool_contract.functions.approve(
            swap_router_contract.address,
            approve_amount_in_wei
        ).build_transaction(txn_data)

        try:
            approve_txn['gas'] = zk_web3.zksync.eth_estimate_gas(approve_txn)
        except Exception as e:
            if 'insufficient balance' in str(e):
                logging.critical(f'[SyncSwap] Insufficient balance to approve {amount * 10} liquidity tokens')
                return enums.TransactionStatus.INSUFFICIENT_BALANCE
            logging.error(f'[SyncSwap] Error while estimating gas: {e}')
            return enums.TransactionStatus.FAILED

        approve_signed = account.sign_transaction(approve_txn)

        approve_tx_hash = zk_web3.eth.send_raw_transaction(approve_signed.rawTransaction)

        logging.info(f'[SyncSwap] Approve transaction: {network.txn_explorer_url}{approve_tx_hash.hex()}')

        approve_receipt = utils.wait_for_transaction_receipt(
            web3=zk_web3.zksync,
            txn_hash=approve_tx_hash,
            logging_prefix='SyncSwap'
        )

        if approve_receipt and approve_receipt['status'] == 1:
            logging.info(f'[SyncSwap] Successfully approved {amount * 10} liquidity tokens')
        else:
            logging.error(f'[SyncSwap] Failed to approve {amount * 10} liquidity tokens')
            return enums.TransactionStatus.FAILED

        txn_data['nonce'] += 1

        utils.random_sleep()

    txn = swap_router_contract.functions.burnLiquiditySingle(
        pool_contract.address,
        amount_in_wei,
        burn_liquidty_data,
        0,
        ZERO_ADDRESS,
        b''
    ).build_transaction(txn_data)

    try:
        txn['gas'] = zk_web3.zksync.eth_estimate_gas(txn)
    except Exception as e:
        if 'insufficient balance' in str(e):
            logging.critical(f'[SyncSwap] Insufficient balance to remove {amount} liquidity tokens')
            return enums.TransactionStatus.INSUFFICIENT_BALANCE
        logging.error(f'[SyncSwap] Error while estimating gas: {e}')
        return enums.TransactionStatus.FAILED

    signed = account.sign_transaction(txn)

    tx_hash = zk_web3.eth.send_raw_transaction(signed.rawTransaction)

    logging.info(f'[SyncSwap] Transaction: {network.txn_explorer_url}{tx_hash.hex()}')

    receipt = utils.wait_for_transaction_receipt(
        web3=zk_web3.zksync,
        txn_hash=tx_hash,
        logging_prefix='SyncSwap'
    )

    if receipt and receipt['status'] == 1:
        logging.info(f'[SyncSwap] Successfully removed {amount} liquidity tokens')
        return enums.TransactionStatus.SUCCESS
    else:
        logging.error(f'[SyncSwap] Failed to remove {amount} liquidity tokens')
        return enums.TransactionStatus.FAILED
