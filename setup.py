from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="auto_replenishment",
    version="1.0.0",
    description="Auto Replenishment — Material Forecast & Material Request for ERPNext",
    author="Printechs",
    author_email="dev@printechs.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires
)
