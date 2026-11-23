from setuptools import setup, find_packages

setup(
    name="datascraper",
    version="1.0",
    packages=find_packages(),
    include_package_data=True,
    package_data={"datascraper": ["*.csv"]},
    entry_points={"scrapy": ["settings = datascraper.settings"]},
)
