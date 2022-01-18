from setuptools import setup

requirements = [
    "aiohttp>=3.7.4.post0,<4",
    "websockets==9.1",
    "pyhumps==1.6.1",
    "web3==5.25.0",
    "orjson==3.4.7"
]

setup(
    name='bxcommon',
    packages=['bxcommon', 'bxutils'],
    package_dir={'': 'src'},
    install_requires=requirements
)
